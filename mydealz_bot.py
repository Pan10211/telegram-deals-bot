#!/usr/bin/env python3
"""
mydealz → Telegram бот.

Два режима в одном:
  1. RSS-парсер: каждые 15 мин публикует новые сделки в канал
  2. Поиск: отвечает на текст/голос в личке — находит лучший пост из последних 200

Установка:
    pip install requests python-telegram-bot rank-bm25 rapidfuzz faster-whisper

config_mydealz.json:
{
  "BOT_TOKEN": "...",
  "CHAT_ID": "-1001234567890",
  "CHANNEL_USERNAME": "mychannel"   // без @, нужен для ссылок на посты
}
"""

import os, re, json, asyncio, logging, tempfile, time
import xml.etree.ElementTree as ET
from html import unescape
from collections import deque
from email.utils import parsedate_to_datetime
from datetime import timezone
from typing import Deque, Dict, Any, List

import requests
from bs4 import BeautifulSoup
from telegram import (Bot, Update, BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, ContextTypes, filters)
from telegram.error import TelegramError
from rank_bm25 import BM25Okapi
from rapidfuzz import fuzz
try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_AVAILABLE = True
except ImportError:
    TRANSLATOR_AVAILABLE = False
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

# ─────────────────────────────────────────
#  КОНФИГУРАЦИЯ
# ─────────────────────────────────────────
CONFIG = {
    "RSS_URL":          "https://www.mydealz.de/rss/alles",
    "POLL_INTERVAL":    15 * 60,
    "FIRST_RUN_COUNT":  5,
    "STATE_FILE":       "mydealz_state.json",
    "CONFIG_FILE":      "config_mydealz.json",
    "POSTS_FILE":       "mydealz_posts.json",
    "KEEP_DAYS":        4,    # сколько дней хранить посты для поиска
}

# ─────────────────────────────────────────
#  ФИЛЬТР ПО КЛЮЧЕВЫМ СЛОВАМ
#  Если список пустой — публикуются ВСЕ объявления.
#  Если задан — только те где заголовок/категория/магазин содержит хотя бы одно слово.
#  Слова регистронезависимы. Можно использовать части слов: "samsung" найдёт "Samsung Galaxy".
# ─────────────────────────────────────────
KEYWORDS: list[str] = []  # пример: ["samsung", "apple", "iphone", "laptop", "monitor"]

WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE     = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE    = os.getenv("WHISPER_COMPUTE", "int8")

class _NoGetUpdates(logging.Filter):
    def filter(self, record):
        return "getUpdates" not in record.getMessage()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
# Убираем спам от httpx о getUpdates
for handler in logging.root.handlers:
    handler.addFilter(_NoGetUpdates())

NS = {
    "media":  "http://search.yahoo.com/mrss/",
    "pepper": "http://www.pepper.com/rss",
}

# ─────────────────────────────────────────
#  ЗАГРУЗКА КОНФИГА
# ─────────────────────────────────────────

def load_credentials() -> dict:
    path = CONFIG["CONFIG_FILE"]
    if not os.path.exists(path):
        log.error(f"Файл {path} не найден!")
        raise SystemExit(1)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for key in ["BOT_TOKEN", "CHAT_ID"]:
        if not data.get(key):
            log.error(f"В {path} отсутствует поле {key}")
            raise SystemExit(1)
    return data

# ─────────────────────────────────────────
#  ХРАНИЛИЩЕ ПОСТОВ ДЛЯ ПОИСКА
# ─────────────────────────────────────────

KEEP_DAYS = 5  # сколько дней хранить посты для поиска
USER_FILTERS_FILE = "user_filters.json"

class UserFilters:
    """Хранит ключевые слова каждого пользователя."""
    def __init__(self, path: str):
        self.path = path
        self.filters: dict[str, list[str]] = {}  # user_id → [keywords]
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.filters = json.load(f)
            except Exception:
                self.filters = {}

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.filters, f, ensure_ascii=False, indent=2)

    def set(self, user_id: int, keywords: list[str]):
        uid = str(user_id)
        if uid not in self.filters:
            self.filters[uid] = {}
        if isinstance(self.filters[uid], list):
            self.filters[uid] = {"keywords": self.filters[uid], "lang": "de"}
        self.filters[uid]["keywords"] = [kw.lower().strip() for kw in keywords if kw.strip()]
        self._save()

    def get(self, user_id: int) -> list[str]:
        v = self.filters.get(str(user_id), [])
        if isinstance(v, list): return v
        return v.get("keywords", [])

    def get_lang(self, user_id: int) -> str:
        v = self.filters.get(str(user_id), {})
        if isinstance(v, list): return "de"
        return v.get("lang", "de")

    def set_lang(self, user_id: int, lang: str):
        uid = str(user_id)
        if uid not in self.filters:
            self.filters[uid] = {}
        if isinstance(self.filters[uid], list):
            self.filters[uid] = {"keywords": self.filters[uid], "lang": lang}
        else:
            self.filters[uid]["lang"] = lang
        self._save()

    def clear(self, user_id: int):
        self.filters.pop(str(user_id), None)
        self._save()

    def matches(self, user_id: int, deal: dict) -> bool:
        keywords = self.get(user_id)
        if not keywords:
            return True  # нет фильтра — показываем всё
        haystack = " ".join([
            deal.get("title", ""),
            deal.get("category", ""),
            deal.get("merchant", ""),
        ]).lower()
        return any(kw in haystack for kw in keywords)

KEEP_DAYS = 5  # сколько дней хранить посты для поиска

class PostStore:
    def __init__(self, path: str):
        self.path  = path
        self.posts: List[Dict[str, Any]] = []
        self._load()

    def _cutoff(self) -> float:
        """Unix timestamp — граница хранения (сейчас минус KEEP_DAYS дней)."""
        return time.time() - KEEP_DAYS * 24 * 3600

    def _prune(self):
        """Удаляем посты старше KEEP_DAYS дней."""
        cutoff = self._cutoff()
        before = len(self.posts)
        self.posts = [p for p in self.posts if p["date"] >= cutoff]
        removed = before - len(self.posts)
        if removed:
            log.info(f"PostStore: удалено {removed} старых постов, осталось {len(self.posts)}")

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.posts = json.load(f)
            self._prune()
            log.info(f"PostStore: загружено {len(self.posts)} постов за последние {KEEP_DAYS} дня")
        except Exception:
            self.posts = []

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.posts, f, ensure_ascii=False, indent=2)

    def add(self, chat_id: int, message_id: int, date_unix: int, text: str, deal_url: str = ""):
        self._prune()
        self.posts.append({
            "chat_id":    chat_id,
            "message_id": message_id,
            "date":       date_unix,
            "text":       text,
            "deal_url":   deal_url,
        })
        self._save()

    def search(self, query: str, top_n: int = 3) -> list:
        """Возвращает top_n наиболее релевантных постов (или пустой список)."""
        self._prune()
        if not self.posts:
            return []
        q_tok = _tokenize(query)
        if not q_tok:
            return []
        docs   = [_tokenize(p["text"]) for p in self.posts]
        scores = BM25Okapi(docs).get_scores(q_tok)
        q_norm = _normalize(query)

        # Считаем combined score для всех
        ranked = []
        for idx, sc in enumerate(scores):
            fuzzy    = fuzz.partial_ratio(q_norm, _normalize(self.posts[idx]["text"]))
            combined = float(sc) + (fuzzy / 100.0) * 2.0
            ranked.append((combined, idx))

        ranked.sort(reverse=True)

        # Минимальный порог релевантности — отсекаем мусор
        MIN_SCORE = 2.0
        results = []
        for score, idx in ranked[:top_n * 2]:
            if score < MIN_SCORE:
                break
            results.append(self.posts[idx])
            if len(results) >= top_n:
                break

        for r in results:
            dt = time.strftime("%d.%m.%Y %H:%M", time.localtime(r["date"]))
            log.info(f"  Найдено: [{dt}] {r['text'][:60]}")
        return results

    def stats(self) -> str:
        self._prune()
        if not self.posts:
            return "Нет постов"
        oldest = time.strftime("%d.%m.%Y %H:%M", time.localtime(min(p["date"] for p in self.posts)))
        newest = time.strftime("%d.%m.%Y %H:%M", time.localtime(max(p["date"] for p in self.posts)))
        return f"{len(self.posts)} постов за {KEEP_DAYS} дня ({oldest} — {newest})"

def translate_to_de(text: str) -> str:
    """Переводим запрос на немецкий если он на русском."""
    if not TRANSLATOR_AVAILABLE:
        return text
    # Определяем язык по наличию кириллицы
    if not re.search(r'[а-яёА-ЯЁ]', text):
        return text  # уже не русский
    try:
        translated = GoogleTranslator(source='ru', target='de').translate(text)
        log.info(f"Перевод: «{text}» → «{translated}»")
        return translated
    except Exception as e:
        log.warning(f"Ошибка перевода: {e}")
        return text

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())

def _tokenize(text: str) -> List[str]:
    text = _normalize(text)
    text = re.sub(r"[^a-z0-9а-яёäöüß]+", " ", text, flags=re.IGNORECASE)
    return [t for t in text.split() if len(t) > 1]

# ─────────────────────────────────────────
#  СОСТОЯНИЕ RSS
# ─────────────────────────────────────────

def load_state() -> dict:
    default = {"last_pubdate": None, "first_run": True, "published_guids": []}
    if os.path.exists(CONFIG["STATE_FILE"]):
        with open(CONFIG["STATE_FILE"], "r", encoding="utf-8") as f:
            default.update(json.load(f))
    return default

def save_state(state: dict):
    with open(CONFIG["STATE_FILE"], "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────
#  ПАРСИНГ RSS
# ─────────────────────────────────────────

def cdata(text) -> str:
    return unescape(text.strip()) if text else ""

def parse_pubdate(s):
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        return None

def get_all_images(thumbnail_url: str, description: str) -> list:
    # Находим все /fs/ картинки — атрибуты могут идти в любом порядке
    img_tags = re.findall(
        r'<[^>]*src="(https://static\.mydealz\.de/threads/raw/[^"]+/fs/[^"]+\.jpg)"[^>]*>',
        description
    )
    imgs = []
    for tag_url in img_tags:
        # Ищем тег целиком чтобы извлечь размеры
        tag_match = re.search(
            r'<[^>]*src="' + re.escape(tag_url) + r'"[^>]*>',
            description
        )
        if not tag_match:
            continue
        tag = tag_match.group(0)
        h_m = re.search(r'data-height="(\d+)"', tag)
        w_m = re.search(r'data-width="(\d+)"', tag)
        if h_m and w_m:
            imgs.append((tag_url, h_m.group(1), w_m.group(1)))
        elif h_m or w_m:
            # Один из атрибутов есть — пробуем извлечь из URL (895x577)
            size_m = re.search(r'/fs/(\d+)x(\d+)/', tag_url)
            if size_m:
                imgs.append((tag_url, size_m.group(2), size_m.group(1)))  # h, w
        else:
            # Размеры в URL
            size_m = re.search(r'/fs/(\d+)x(\d+)/', tag_url)
            if size_m:
                imgs.append((tag_url, size_m.group(2), size_m.group(1)))
    result = []
    if imgs:
        for idx, (url, h, w) in enumerate(imgs):
            h, w = int(h), int(w)
            ratio = w / h
            reason = None
            if h < 400:       reason = f"h={h} < 400"
            elif w < 400:     reason = f"w={w} < 400"
            elif ratio > 3.0: reason = f"ratio={ratio:.2f} > 3.0"

        filtered = [(url, int(h), int(w)) for url, h, w in imgs
                    if int(h) >= 400 and int(w) >= 400 and int(w)/int(h) <= 3.0]
        # Приоритет: квадратные (товарные фото) → немного широкие → широкие
        square = [(url, h, w) for url, h, w in filtered if w/h <= 1.2]   # почти квадрат
        normal = [(url, h, w) for url, h, w in filtered if 1.2 < w/h <= 1.4]
        wide   = [(url, h, w) for url, h, w in filtered if w/h > 1.4]
        # Графики DealCheck обычно 895x577 (ratio≈1.55) — они попадают в wide
        # Если есть квадратные или нормальные — берём их первыми
        ordered = square + normal + wide
        for url, h, w in ordered[:3]:
            result.append(url)
        # Thumbnail из RSS — главная картинка товара, добавляем если ещё нет в списке
        if thumbnail_url:
            thumb_up = re.sub(r'/re/\d+x\d+/qt/\d+/', '/re/768x768/qt/70/', thumbnail_url)
            # Извлекаем ID файла для сравнения (без CDN-префикса)
            thumb_id = re.search(r'/(\d+_\d+)/', thumbnail_url)
            already  = any((re.search(r'/(\d+_\d+)/', u) or [None, ''])[0] ==
                           (thumb_id.group(1) if thumb_id else '') for u in result)
            if not already:
                result.insert(0, thumb_up)  # ставим первой
        if not result and thumbnail_url:
            upgraded = re.sub(r'/re/\d+x\d+/qt/\d+/', '/re/768x768/qt/70/', thumbnail_url)
            result.append(upgraded)
    else:
        if thumbnail_url:
            upgraded = re.sub(r'/re/\d+x\d+/qt/\d+/', '/re/768x768/qt/70/', thumbnail_url)
            result.append(upgraded)
    return result[:3]

def _parse_val(p: str) -> float:
    """Конвертируем строку цены в float: '649€' → 649.0, '1.299,99€' → 1299.99"""
    p = re.sub(r'[^\d.,]', '', p)
    # немецкий формат: 1.299,99 → убираем точку-разделитель тысяч, запятую→точка
    if ',' in p and '.' in p:
        p = p.replace('.', '').replace(',', '.')
    elif ',' in p:
        p = p.replace(',', '.')
    try:
        return float(p)
    except Exception:
        return 0.0

def _parse_price_info_verbose(description: str, title: str, next_best_price: str, current_price: str = ""):
    """Только логирует все найденные цены — без возврата значений."""
    full = description + " " + title

    if next_best_price:
        log.info(f"  [цена] nextBestPrice (pepper RSS): {next_best_price!r}")

    m = re.search(r'<(?:s|del|strike)[^>]*>\s*([\d.,]+\s*€)\s*</(?:s|del|strike)>', full, re.IGNORECASE)
    if m:
        log.info(f"  [цена] strikethrough <s>/<del>: {m.group(1).strip()!r}")

    m = re.search(r'UVP[^>]*>\s*([\d.,]+\s*€)', full, re.IGNORECASE)
    if m:
        log.info(f"  [цена] UVP_html: {m.group(1).strip()!r}")

    m = re.search(r'UVP\s*:?\s*([\d.,]+\s*€)', full, re.IGNORECASE)
    if m:
        log.info(f"  [цена] UVP_text: {m.group(1).strip()!r}")

    m = re.search(r'(?:statt|war|anstatt|instead of|RRP|VK)\s*:?\s*([\d.,]+\s*€)', full, re.IGNORECASE)
    if m:
        log.info(f"  [цена] statt/war/RRP: {m.group(1).strip()!r}")

    m = re.search(r'-(\d+)%', full)
    if m:
        log.info(f"  [цена] скидка -N%: -{m.group(1)}%")

    m = re.search(r'(\d+)%\s*(?:gespart|Rabatt|reduziert|off|günstiger)', full, re.IGNORECASE)
    if m:
        log.info(f"  [цена] скидка слово: -{m.group(1)}%")

    all_prices = list(dict.fromkeys(p.strip() for p in re.findall(r'[\d]+[.,][\d]+\s*€|[\d]+\s*€', full)))
    if all_prices:
        log.info(f"  [цена] все в тексте: {all_prices[:8]}")


def parse_price_info(description: str, title: str = "", next_best_price: str = "") -> tuple:
    old_price, discount = "", ""
    full = description + " " + title

    candidates = {}  # источник → найденная цена

    # 0. nextBestPrice из pepper RSS
    if next_best_price:
        candidates["nextBestPrice"] = next_best_price.strip()

    # 1. Зачёркнутая цена <s>/<del>/<strike>
    m = re.search(r'<(?:s|del|strike)[^>]*>\s*([\d.,]+\s*€)\s*</(?:s|del|strike)>', full, re.IGNORECASE)
    if m:
        candidates["strikethrough"] = m.group(1).strip()

    # 2. UVP в HTML-теге или тексте
    m = re.search(r'UVP[^>]*>\s*([\d.,]+\s*€)', full, re.IGNORECASE)
    if m:
        candidates["UVP_html"] = m.group(1).strip()
    m = re.search(r'UVP\s*:?\s*([\d.,]+\s*€)', full, re.IGNORECASE)
    if m:
        candidates["UVP_text"] = m.group(1).strip()

    # 3. statt / war / anstatt / RRP / VK
    m = re.search(r'(?:statt|war|anstatt|instead of|UVP|RRP|VK)\s*:?\s*([\d.,]+\s*€)', full, re.IGNORECASE)
    if m:
        candidates["statt"] = m.group(1).strip()

    # 4. inkl. Versand
    m = re.search(r'">(\d[\d.,]+\s*€)\s*inkl', full, re.IGNORECASE)
    if m:
        candidates["inkl_versand"] = m.group(1).strip()

    # Все цены в тексте
    # Выбираем по приоритету
    for src in ["strikethrough", "UVP_html", "UVP_text", "statt", "inkl_versand", "nextBestPrice"]:
        if src in candidates:
            old_price = re.sub(r'\s*inkl\.?\s*Versand\.?', '', candidates[src]).strip()
            break

    # Скидка
    discount_candidates = {}
    m2 = re.search(r'-(\d+)%', full)
    if m2: discount_candidates["minus_%"] = f"-{m2.group(1)}%"
    m2 = re.search(r'(\d+)%\s*(?:gespart|Rabatt|reduziert|off|günstiger|sparen|billiger)', full, re.IGNORECASE)
    if m2: discount_candidates["rabatt_word"] = f"-{m2.group(1)}%"

    # Вычисляем старую цену если есть текущая цена и скидка в %
    # Например: price=898€, скидка -8% → old = 898 / (1 - 0.08) ≈ 976€
    if not old_price and next_best_price:
        old_price = next_best_price.strip()

    # Если есть скидка % и цена товара — вычисляем старую цену
    if not old_price and discount_candidates:
        pct_str = discount_candidates.get("minus_%") or discount_candidates.get("rabatt_word", "")
        pct_m = re.search(r"(\d+)", pct_str)
        # Ищем текущую цену из pepper:merchant (передаётся как параметр price в build_caption)
        price_m = re.search(r"(\d[\d.,]+)\s*€", full[:100])  # берём первую цену в тексте
        if pct_m and price_m:
            try:
                pct      = int(pct_m.group(1))
                curr     = _parse_val(price_m.group(0))
                if 1 <= pct <= 90 and curr > 0:
                    orig = curr / (1 - pct / 100)
                    old_price = f"{orig:.0f}€"
            except Exception:
                pass

    # Берём скидку по приоритету
    for src in ["minus_%", "rabatt_word"]:
        if src in discount_candidates:
            discount = discount_candidates[src]
            break

    # Если нашли и старую и новую цену — вычислим скидку если её нет
    if not discount and old_price:
        price_m2 = re.search(r"(\d[\d.,]+)\s*€", full[:100])
        if price_m2:
            try:
                curr2 = _parse_val(price_m2.group(0))
                orig2 = _parse_val(old_price)
                if orig2 > curr2 > 0:
                    pct2 = round((orig2 - curr2) / orig2 * 100)
                    if 1 <= pct2 <= 90:
                        discount = f"-{pct2}%"
            except Exception:
                pass

    return old_price, discount

def deal_matches_keywords(deal: dict) -> bool:
    """Проверяем совпадение с ключевыми словами. Если KEYWORDS пуст — пропускаем всё."""
    if not KEYWORDS:
        return True
    # Собираем текст для поиска: заголовок + категория + магазин
    haystack = " ".join([
        deal.get("title", ""),
        deal.get("category", ""),
        deal.get("merchant", ""),
    ]).lower()
    for kw in KEYWORDS:
        if kw.lower() in haystack:
            log.info(f"  ✅ Совпадение по ключевому слову «{kw}»")
            return True
    log.info(f"  ⏭ Пропущено (не по теме): {deal.get('title','')[:60]}")
    return False

def scrape_price_from_page(url: str) -> dict:
    """
    Парсим страницу сделки — извлекаем цену, старую цену и скидку.
    Возвращает dict с ключами: price, old_price, discount (все строки).
    """
    result = {"price": "", "old_price": "", "discount": ""}
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        # 1. HTML — зачёркнутая цена (актуальна, не кешируется)
        for sel in ["s.mojo-price", "del.mojo-price", ".mojo-price--crossed",
                    "s[class*=price]", "del[class*=price]", ".crossed-price",
                    "s", "del"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if re.search(r"\d+[,.]\d+\s*€", t):
                    result["old_price"] = t
                    log.info(f"  [page] old_price из HTML [{sel}]: {t!r}")
                    break

        # 2. HTML — скидка % из бейджа (актуальна)
        for sel in [".cept-badge--hot", ".cept-badge", "[class*=badge--green]",
                    "[class*=badge--hot]", "[class*=badge--percent]",
                    "[class*=discount]", "[class*=saving]", "[class*=badge]"]:
            for el in soup.select(sel):
                t = el.get_text(strip=True)
                m = re.search(r"(-?\d+)%", t)
                if m:
                    pct = int(m.group(1))
                    if 1 <= abs(pct) <= 90:
                        result["discount"] = f"-{abs(pct)}%"
                        log.info(f"  [page] discount из HTML [{sel}]: {result['discount']!r}")
                        break
            if result["discount"]:
                break

        # 3. HTML — текущая цена
        for sel in [".mojo-price", "[class*=thread-price]", "[class*=deal-price]",
                    "[class*=price--main]", ".cept-thread-price-amount"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if re.search(r"\d+[,.]\d+\s*€", t):
                    result["price"] = t
                    log.info(f"  [page] price из HTML [{sel}]: {t!r}")
                    break

        # 4. FALLBACK — script JSON (может быть кешированным, используем только
        #    если HTML ничего не дал)
        if not result["price"] or not result["old_price"]:
            for script in soup.find_all("script"):
                t = script.string or ""
                if '"price"' not in t or '"nextBestPrice"' not in t:
                    continue
                m_price = re.search(r'"price"\s*:\s*([0-9]+\.?[0-9]*)', t)
                m_old   = re.search(r'"nextBestPrice"\s*:\s*([0-9]+\.?[0-9]*)', t)
                m_pct   = re.search(r'"percentage"\s*:\s*([0-9]+)', t)
                if m_price and float(m_price.group(1)) > 0:
                    if not result["price"]:
                        result["price"] = f"{float(m_price.group(1)):.2f}".replace(".", ",") + "€"
                        log.info(f"  [page] price из script (fallback): {result['price']!r}")
                    if m_old and not result["old_price"]:
                        o = float(m_old.group(1))
                        if o > 0:
                            result["old_price"] = f"{o:.2f}".replace(".", ",") + "€"
                            log.info(f"  [page] old_price из script (fallback): {result['old_price']!r}")
                    if m_pct and not result["discount"]:
                        pct = int(m_pct.group(1))
                        if pct > 0:
                            result["discount"] = f"-{pct}%"
                            log.info(f"  [page] discount из script (fallback): {result['discount']!r}")
                    break

        # 5. Если есть обе цены но нет скидки — вычисляем
        if result["price"] and result["old_price"] and not result["discount"]:
            try:
                curr = _parse_val(result["price"])
                orig = _parse_val(result["old_price"])
                if orig > curr > 0:
                    pct = round((orig - curr) / orig * 100)
                    if 1 <= pct <= 90:
                        result["discount"] = f"-{pct}%"
                        log.info(f"  [page] discount вычислен: {result['discount']!r}")
            except Exception:
                pass

        log.info(f"  [page] price={result['price']!r} "
                 f"old={result['old_price']!r} disc={result['discount']!r}")

    except Exception as e:
        log.warning(f"  [page] Ошибка парсинга {url}: {e}")
    return result

def fetch_deals() -> list:
    try:
        resp = requests.get(CONFIG["RSS_URL"], timeout=15,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        log.warning(f"Ошибка загрузки RSS: {e}")
        return []

    deals = []
    channel = root.find("channel")
    if not channel:
        return []

    for item in channel.findall("item"):
        def get(tag):
            el = item.find(tag)
            return cdata(el.text) if el is not None else ""

        title    = get("title")
        link     = get("link")
        guid     = get("guid") or link
        pub_date = get("pubDate")
        category = get("category")
        pub_dt   = parse_pubdate(pub_date)

        merchant_el = item.find(f"{{{NS['pepper']}}}merchant")
        merchant        = merchant_el.get("name", "")          if merchant_el is not None else ""
        price_rss       = merchant_el.get("price", "")         if merchant_el is not None else ""
        next_best_price = merchant_el.get("nextBestPrice", "") if merchant_el is not None else ""

        description = get("description")

        # Цены из RSS (быстро) — страница будет спаршена при отправке
        price     = price_rss
        # next_best_price — цена конкурента, НЕ старая цена; передаём в parse_price_info
        old_price_rss, discount = parse_price_info(description, title, next_best_price)
        old_price = old_price_rss

        thumbnail_url = ""
        for ns_tag in [f"{{{NS['media']}}}content", f"{{{NS['media']}}}thumbnail"]:
            el = item.find(ns_tag)
            if el is not None:
                thumbnail_url = el.get("url", "")
                if thumbnail_url:
                    break

        all_images = get_all_images(thumbnail_url, description)
        image_url  = all_images[0] if all_images else ""

        deals.append({
            "title": title, "link": link, "guid": guid,
            "image_url": image_url, "all_images": all_images,
            "merchant": merchant, "price": price,
            "old_price": old_price, "discount": discount,
            "category": cdata(category),
            "pub_date": pub_date, "pub_dt": pub_dt,
            "_desc": description, "_nbp": next_best_price,
            
        })
    return deals

# ─────────────────────────────────────────
#  ФОРМАТИРОВАНИЕ И ОТПРАВКА СДЕЛКИ
# ─────────────────────────────────────────

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def build_caption(deal: dict) -> str:
    lines = [f"<b>{esc(deal['title'])}</b>", ""]
    price_parts = []
    price     = deal.get("price", "")
    old_price = deal.get("old_price", "")
    discount  = deal.get("discount", "")

    def to_float(s):
        try:
            return float(re.sub(r"[^\d.,]", "", s).replace(",", "."))
        except Exception:
            return 0.0

    # Если старая цена меньше текущей — они перепутаны, меняем местами
    if price and old_price:
        pv = to_float(price)
        ov = to_float(old_price)
        if ov > 0 and pv > 0 and ov < pv:
            price, old_price = old_price, price

    if price:     price_parts.append(f"💰 <b>{esc(price)}</b>")
    if old_price: price_parts.append(f"<s>{esc(old_price)}</s>")
    if discount:  price_parts.append(f"🔥 <b>{esc(discount)}</b>")
    if price_parts: lines.append("  ".join(price_parts))
    if deal.get("merchant"):  lines.append(f"🛒 {esc(deal['merchant'])}")
    if deal.get("category"):  lines.append(f"🏷 {esc(deal['category'])}")
    return "\n".join(lines)

def _download_image_sync(url: str):
    """Синхронная загрузка — вызывать только через run_in_executor."""
    if not url: return None
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    def variants(u):
        if "/fs/" in u:
            return [re.sub(r'/fs/\d+x\d+/qt/\d+/', f'/fs/895x577/qt/{q}/', u) for q in [75, 65]] + [u]
        base = re.sub(r'/re/\d+x\d+/qt/\d+/', '/re/{s}/qt/60/', u)
        return [base.replace("{s}", s) for s in ["400x400","300x300","200x200","150x150","100x100"]] + [u]
    for candidate in variants(url):
        try:
            resp = requests.get(candidate, timeout=8, headers=headers)
            if resp.status_code == 200:
                return resp.content
        except Exception:
            continue
    log.warning(f"Не удалось скачать картинку: {url}")
    return None


async def download_image(url: str):
    """Асинхронная обёртка — не блокирует event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_image_sync, url)
    return None

async def send_deal(bot: Bot, deal: dict, chat_id: str):
    log.info(f"━━ {deal['title'][:65]}")
    # Повторно парсим цены с подробным логированием — здесь они будут рядом с публикацией
    _parse_price_info_verbose(deal.get("_desc",""), deal["title"], deal.get("_nbp",""), deal.get("price",""))

    log.info(f"   💰 итог: цена={deal.get('price','')}  старая={deal.get('old_price','')}  скидка={deal.get('discount','')}")

    all_images = deal.get("all_images", [deal["image_url"]] if deal["image_url"] else [])
    image_url  = all_images[0] if all_images else ""
    log.info(f"   🖼  {image_url or 'нет картинки'}")

    caption      = build_caption(deal)
    bot_username = deal.get("bot_username", "")
    buttons = [InlineKeyboardButton("🛍 Zum Deal", url=deal["link"])]
    if bot_username:
        buttons.append(InlineKeyboardButton("🤖 Menü (Filter/Suche)", url=f"https://t.me/{bot_username}"))
    keyboard = InlineKeyboardMarkup([buttons])

    img_bytes = await download_image(image_url) if image_url else None
    try:
        if img_bytes:
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=img_bytes,
                caption=caption[:1024],
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=caption[:4096],
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        log.info(f"✅ {deal['price']:>8}  {deal['title'][:55]}")
        return msg.message_id
    except TelegramError as e:
        log.error(f"Ошибка: {e}  [{deal['title'][:40]}]")
        return None

# ─────────────────────────────────────────
#  ПОИСК — ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────

def post_link(message_id: int, channel_username: str) -> str | None:
    if not channel_username:
        return None
    return f"https://t.me/{channel_username}/{message_id}"

def detect_language(text: str) -> str:
    """Простое определение языка по символам."""
    if re.search(r"[а-яёА-ЯЁ]", text):
        return "ru"
    # По умолчанию немецкий — бот немецкий
    return "de"

def not_found_message(query_text: str, uid: int = 0, uf=None) -> str:
    return T(uid, "no_results", uf, query_text) if uf else f"Keine Ergebnisse für «{query_text}» gefunden."

async def reply_with_best(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           query_text: str, store: PostStore, channel_username: str):
    # Переводим запрос на немецкий если нужно
    search_query = translate_to_de(query_text)
    if search_query != query_text:
        await update.message.reply_text(f"🔄 Ищу по-немецки: «{search_query}»")

    uid   = update.message.from_user.id
    uf    = context.bot_data.get("user_filters")
    items = store.search(search_query, top_n=3)

    # Применяем фильтр пользователя — сужаем базу до интересных товаров
    if uf:
        filtered = [i for i in items
                    if uf.matches(uid, {"title": i.get("text",""), "category": "", "merchant": ""})]
        # Если фильтр есть но ничего не нашлось — ищем по всей базе
        kw = uf.get(uid)
        if kw and not filtered:
            await update.message.reply_text(T(uid, "filter_no_match", uf, ", ".join(kw)))
        elif kw and filtered:
            items = filtered

    if not items:
        await update.message.reply_text(not_found_message(query_text, uid, uf))
        return

    # Отправляем до 3 найденных постов
    await update.message.reply_text(T(uid, "found", uf, query_text, len(items)))
    for item in items:
        tg_post = post_link(item["message_id"], channel_username)
        buttons = []
        if item.get("deal_url"):
            buttons.append(InlineKeyboardButton("🛍 Zum Deal", url=item["deal_url"]))
        if tg_post:
            buttons.append(InlineKeyboardButton("📢 Zum Kanal", url=tg_post))
        keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
        try:
            await context.bot.copy_message(
                chat_id=update.message.chat_id,
                from_chat_id=item["chat_id"],
                message_id=item["message_id"],
                reply_markup=keyboard,
            )
        except Exception:
            dt  = time.strftime("%d.%m.%Y %H:%M", time.localtime(item["date"]))
            txt = item["text"][:500] + ("…" if len(item["text"]) > 500 else "")
            await update.message.reply_text(txt, reply_markup=keyboard)
        await asyncio.sleep(0.3)

# ─────────────────────────────────────────
#  ФОНОВАЯ ЗАДАЧА — RSS ЦИКЛ
# ─────────────────────────────────────────

async def notify_users(bot: Bot, deal: dict, msg_id: int, chat_id: int,
                       user_filters: UserFilters, channel_username: str):
    """Отправляем личное уведомление пользователям у которых фильтр совпал с постом."""
    deal_text = f"{deal.get('title','')} {deal.get('price','')} {deal.get('merchant','')} {deal.get('category','')}"
    deal_for_match = {"title": deal_text, "category": deal.get("category",""), "merchant": deal.get("merchant","")}

    notified = 0
    for uid_str in user_filters.filters:
        keywords = user_filters.get(int(uid_str))  # правильно извлекаем список
        if not keywords:
            continue
        # Проверяем совпадение
        haystack = deal_text.lower()
        if not any(kw.lower() in haystack for kw in keywords):
            continue
        # Совпало — отправляем в личку
        try:
            uid = int(uid_str)
            tg_link = post_link(msg_id, channel_username)
            buttons = []
            if deal.get("link"):
                buttons.append(InlineKeyboardButton("🛍 Zum Deal", url=deal["link"]))
            if tg_link:
                buttons.append(InlineKeyboardButton("📢 Zum Kanal", url=tg_link))
            keyboard = InlineKeyboardMarkup([buttons]) if buttons else None

            kw_matched = [kw for kw in keywords if kw.lower() in haystack]
            header = f"🔔 По вашему фильтру <b>{', '.join(kw_matched)}</b>:\n\n"

            await bot.copy_message(
                chat_id=uid,
                from_chat_id=chat_id,
                message_id=msg_id,
                reply_markup=keyboard,
            )
            # Подпись с указанием ключевого слова
            await bot.send_message(
                chat_id=uid,
                text=header + f"💡 /filter — изменить фильтр",
                parse_mode="HTML",
            )
            notified += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            log.warning(f"[notify] uid={uid_str}: {e}")

    if notified:
        log.info(f"[notify] «{deal['title'][:40]}» → {notified} пользователей")

async def rss_loop(bot: Bot, chat_id: str, store: PostStore, bot_username: str = "", user_filters: "UserFilters | None" = None, channel_username: str = ""):
    log.info(f"▶ rss_loop стартовал, chat_id={chat_id}")
    state = load_state()

    while True:
        try:
            # ── Загрузка RSS ──────────────────────────────
            deals = fetch_deals()
            if not deals:
                log.warning("RSS вернул пустой список.")
                await asyncio.sleep(CONFIG["POLL_INTERVAL"])
                continue

            published_guids = set(state.get("published_guids", []))

            if state["first_run"]:
                candidates = [d for d in deals if d["guid"] not in published_guids]
                to_publish  = candidates[:CONFIG["FIRST_RUN_COUNT"]]
                for deal in reversed(to_publish):
                    deal["bot_username"] = bot_username
                    msg_id = await send_deal(bot, deal, chat_id)
                    published_guids.add(deal["guid"])
                    store.add(
                        chat_id=int(chat_id),
                        message_id=msg_id or 0,
                        date_unix=int(deal["pub_dt"].timestamp()) if deal["pub_dt"] else int(time.time()),
                        text=f"{deal['title']} {deal.get('price','')} {deal.get('merchant','')} {deal.get('category','')}",
                        deal_url=deal.get("link", ""),
                    )
                    await asyncio.sleep(2)
                newest = deals[0]
                state.update({"last_pubdate": newest["pub_date"], "first_run": False,
                              "published_guids": list(published_guids)[-200:]})
                log.info(f"Первый запуск: опубликовано {len(to_publish)}")

            else:
                last_dt   = parse_pubdate(state["last_pubdate"]) if state["last_pubdate"] else None
                new_deals = ([d for d in deals
                              if d["pub_dt"] and d["pub_dt"] > last_dt
                              and d["guid"] not in published_guids]
                             if last_dt else [])
                if new_deals:
                    for deal in reversed(new_deals):
                        deal["bot_username"] = bot_username
                        msg_id = await send_deal(bot, deal, chat_id)
                        published_guids.add(deal["guid"])
                        store.add(
                            chat_id=int(chat_id),
                            message_id=msg_id or 0,
                            date_unix=int(deal["pub_dt"].timestamp()) if deal["pub_dt"] else int(time.time()),
                            text=f"{deal['title']} {deal.get('price','')} {deal.get('merchant','')} {deal.get('category','')}",
                            deal_url=deal.get("link", ""),
                        )
                        # Уведомляем пользователей у которых совпал фильтр
                        if msg_id and user_filters:
                            await notify_users(bot, deal, msg_id, int(chat_id), user_filters, channel_username)
                        await asyncio.sleep(2)
                    state.update({"last_pubdate": deals[0]["pub_date"],
                                  "published_guids": list(published_guids)[-200:]})
                    log.info(f"Опубликовано {len(new_deals)} сделок.")
                else:
                    log.info("Новых сделок нет.")

            save_state(state)

        except Exception as e:
            err = str(e)
            if any(x in err for x in ["getaddrinfo", "ConnectError", "NetworkError", "Timeout"]):
                log.error(f"⚠️ Нет интернета ({type(e).__name__}). Жду 15 мин...")
            else:
                log.exception(f"⚠️ Ошибка в RSS цикле: {e}")

        log.info(f"⏳ Жду {CONFIG['POLL_INTERVAL'] // 60} мин...")
        await asyncio.sleep(CONFIG["POLL_INTERVAL"])

# ─────────────────────────────────────────
#  TELEGRAM HANDLERS (ПОИСК)
# ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = context.bot_data["store"].stats()
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("🔧 Filter"), KeyboardButton("🔎 Suche")]],
        resize_keyboard=True,
        is_persistent=True,
    )
    await update.message.reply_text(
        "🇩🇪 <b>DealCheck Suchbot</b>\n"
        "Ich durchsuche die letzten 5 Tage Deals.\n"
        "• Suche: tippe <code>Kaffee</code> oder /find kaffee\n"
        "• Filter: /filter kaffee (mehrere Wörter möglich)\n\n"
        "🇬🇧 <b>DealCheck Search Bot</b>\n"
        "I search deals from the last 5 days.\n"
        "• Search: type <code>coffee</code> or /find coffee\n"
        "• Filter: /filter coffee (multiple words allowed)\n\n"
        "🇷🇺 <b>Бот поиска DealCheck</b>\n"
        "Ищу среди сделок за последние 5 дней.\n"
        "• Поиск: напиши <code>кофе</code> или /find кофе\n"
        "• Фильтр: /filter кофе (можно несколько слов через пробел)\n\n"
        f"📊 {stats}",
        parse_mode="HTML",
        reply_markup=kb,
    )

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Напиши так: /find iphone 15")
        return
    store: PostStore = context.bot_data["store"]
    ch = context.bot_data["channel_username"]
    await reply_with_best(update, context, query, store, ch)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        return

    # Не обрабатываем команды — их ловит CommandHandler
    if q.startswith("/"):
        return

    # Если это ответ на ForceReply от кнопки "добавить слово" — обрабатываем как /filter
    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.from_user.is_bot:
        reply_text = msg.reply_to_message.text or ""
        if any(x in reply_text for x in ["Wörter eingeben", "Type words", "Введите слова"]):
            context.args = q.split()
            return await cmd_filter(update, context)

    # Кнопки reply-клавиатуры
    if q == "🔧 Filter":
        return await cmd_filter(update, context)
    if q == "🔎 Suche":
        await update.message.reply_text(
            "🔎 Напишите название товара:\n<code>Samsung TV</code>",
            parse_mode="HTML"
        )
        return

    # Поиск
    store: PostStore = context.bot_data["store"]
    ch = context.bot_data["channel_username"]
    await reply_with_best(update, context, q, store, ch)

# Переводы интерфейса
STRINGS = {
    "de": {
        "filter_title":    "🔧 <b>Dein Filter:</b>",
        "filter_none":     "🔧 <b>Kein Filter gesetzt</b> — alle Angebote werden angezeigt.",
        "filter_hint":     "Kategorie wählen oder Wort eingeben:\n<code>/filter samsung</code>",
        "filter_delete":   "Tippe ❌ um ein Wort zu löschen.",
        "delete_all":      "🗑 Alle löschen",
        "done":            "✅ Fertig",
        "add_word":        "➕ Wort zum Filter hinzufügen",
        "quick_select":    "─── Schnellauswahl ───",
        "add_hint":        "✏️ Schreib das Wort als Befehl:\n<code>/filter samsung</code>\n\nMehrere Wörter:\n<code>/filter iphone macbook ipad</code>",
        "saved":           "✅ Gespeichert",
        "deleted":         "🗑 Gelöscht",
        "added":           "✅ Hinzugefügt",
        "lang_select":     "🌐 Sprache / Language / Язык",
        "no_results":      "Keine Ergebnisse für «{}» gefunden.",
        "found":           "🔎 «{}» — {} gefunden:",
        "filter_active":   "Filter aktiv: {}. Suche nur darin...",
        "filter_no_match": "Kein Treffer im Filter ({}). Suche in allen...",
    },
    "en": {
        "filter_title":    "🔧 <b>Your filter:</b>",
        "filter_none":     "🔧 <b>No filter set</b> — all deals are shown.",
        "filter_hint":     "Choose a category or type a word:\n<code>/filter samsung</code>",
        "filter_delete":   "Tap ❌ to remove a word.",
        "delete_all":      "🗑 Delete all",
        "done":            "✅ Done",
        "add_word":        "➕ Add word to filter",
        "quick_select":    "─── Quick select ───",
        "add_hint":        "✏️ Type the word as command:\n<code>/filter samsung</code>\n\nMultiple words:\n<code>/filter iphone macbook ipad</code>",
        "saved":           "✅ Saved",
        "deleted":         "🗑 Deleted",
        "added":           "✅ Added",
        "lang_select":     "🌐 Sprache / Language / Язык",
        "no_results":      "Nothing found for «{}».",
        "found":           "🔎 «{}» — found {}:",
        "filter_active":   "Filter active: {}. Searching within...",
        "filter_no_match": "No match in filter ({}). Searching all...",
    },
    "ru": {
        "filter_title":    "🔧 <b>Ваш фильтр:</b>",
        "filter_none":     "🔧 <b>Фильтр не установлен</b> — показываются все товары.",
        "filter_hint":     "Выберите категорию или введите слово:\n<code>/filter samsung</code>",
        "filter_delete":   "Нажмите ❌ чтобы удалить слово.",
        "delete_all":      "🗑 Удалить все",
        "done":            "✅ Готово",
        "add_word":        "➕ Добавить слово в фильтр",
        "quick_select":    "─── Быстрый выбор ───",
        "add_hint":        "✏️ Напишите слово командой:\n<code>/filter samsung</code>\n\nНесколько слов:\n<code>/filter iphone macbook ipad</code>",
        "saved":           "✅ Сохранено",
        "deleted":         "🗑 Удалено",
        "added":           "✅ Добавлено",
        "lang_select":     "🌐 Sprache / Language / Язык",
        "no_results":      "По запросу «{}» ничего не найдено.",
        "found":           "🔎 «{}» — найдено {}:",
        "filter_active":   "Фильтр активен: {}. Ищу в нём...",
        "filter_no_match": "В фильтре ({}) не найдено. Ищу по всей базе...",
    },
}

def T(user_id: int, key: str, uf: "UserFilters", *args) -> str:
    lang = uf.get_lang(user_id) if uf else "de"
    s = STRINGS.get(lang, STRINGS["de"]).get(key, STRINGS["de"].get(key, key))
    return s.format(*args) if args else s

# Предустановленные категории
# Предустановленные категории
# label → (display_name, keywords)
FILTER_CATEGORIES = {
    "smartphones": ("📱 Smartphones", ["iphone", "samsung galaxy", "pixel", "xiaomi"]),
    "laptops":     ("💻 Laptops",     ["laptop", "notebook", "macbook", "lenovo"]),
    "monitore":    ("🖥 Monitore",    ["monitor", "display", "bildschirm"]),
    "gaming":      ("🎮 Gaming",      ["ps5", "xbox", "nintendo", "gaming", "gpu", "rtx"]),
    "apple":       ("🍎 Apple",       ["iphone", "ipad", "macbook", "airpods"]),
    "haushalt":    ("🏠 Haushalt",    ["bosch", "siemens", "miele", "waschmaschine"]),
    "mode":        ("👟 Mode",        ["nike", "adidas", "puma", "schuhe"]),
    "foto":        ("📷 Foto/Video",  ["kamera", "camera", "gopro", "sony alpha"]),
}

def build_filter_menu(uid: int, uf: "UserFilters") -> tuple:
    current = uf.get(uid)
    lang    = uf.get_lang(uid)

    if current:
        kw_list = "  ".join(f"<code>{k}</code>" for k in current)
        text = T(uid, "filter_title", uf) + f"\n{kw_list}\n\n" + T(uid, "filter_delete", uf)
    else:
        text = T(uid, "filter_none", uf) + "\n\n" + T(uid, "filter_hint", uf)

    buttons = []

    # Выбор языка — всегда вверху
    lang_buttons = []
    for code, flag in [("de", "🇩🇪"), ("en", "🇬🇧"), ("ru", "🇷🇺")]:
        label = f"{flag} ✓" if lang == code else flag
        lang_buttons.append(InlineKeyboardButton(label, callback_data=f"flang:{code}"))
    buttons.append(lang_buttons)

    # Активные слова с крестиком
    if current:
        row = []
        for kw in current:
            row.append(InlineKeyboardButton(f"❌ {kw}", callback_data=f"fdel:{kw}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([
            InlineKeyboardButton(T(uid, "delete_all", uf), callback_data="filter_clear"),
            InlineKeyboardButton(T(uid, "done", uf),       callback_data="fdone"),
        ])

    buttons.append([InlineKeyboardButton(T(uid, "add_word", uf), callback_data="fadd")])

    # Категории
    buttons.append([InlineKeyboardButton(T(uid, "quick_select", uf), callback_data="noop")])
    row = []
    for key, (display, _) in FILTER_CATEGORIES.items():
        row.append(InlineKeyboardButton(display, callback_data=f"fcat:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    return text, InlineKeyboardMarkup(buttons)

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uf: UserFilters = context.bot_data["user_filters"]
    uid = update.message.from_user.id
    raw = " ".join(context.args)
    args = [a.strip().lower() for a in re.split(r"[,\s]+", raw) if a.strip()]

    if args:
        existing = uf.get(uid)
        merged = list(dict.fromkeys(existing + args))
        uf.set(uid, merged)

    text, keyboard = build_filter_menu(uid, uf)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

async def cmd_filter_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uf: UserFilters = context.bot_data["user_filters"]
    uf.clear(update.message.from_user.id)
    await update.message.reply_text("\u2705 Фильтр убран — показываются все товары.")

async def on_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uf: UserFilters = context.bot_data["user_filters"]
    uid = query.from_user.id
    data = query.data

    if data == "noop":
        await query.answer()
        return

    if data == "fdone":
        kw = uf.get(uid) or []
        kw_list = ", ".join(kw) if kw else "—"
        await query.answer(f"✅ Сохранено: {kw_list[:30]}")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if data == "fadd":
        await query.answer()
        hints = {
            "de": "Wörter eingeben (z.B.: kaffee samsung):",
            "en": "Type words (e.g.: coffee samsung):",
            "ru": "Введите слова (например: кофе samsung):",
        }
        lang = uf.get_lang(uid)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=hints.get(lang, hints["de"]),
            reply_markup=ForceReply(selective=True, input_field_placeholder="/filter кофе samsung"),
        )
        return

    if data == "filter_clear":
        uf.clear(uid)
        await query.answer("Фильтр удалён")

    elif data.startswith("fdel:"):
        kw = data.split("fdel:", 1)[1]
        uf.set(uid, [k for k in uf.get(uid) if k != kw])
        await query.answer(f"Удалено: {kw}")

    elif data.startswith("flang:"):
        lang = data.split("flang:", 1)[1]
        if lang in ("de", "en", "ru"):
            uf.set_lang(uid, lang)
            CMDS = {
                "de": [("start", "ℹ️ Hilfe und Anleitung"), ("filter", "🔧 Filter einrichten"),
                       ("filter_clear", "❌ Filter löschen"), ("find", "🔎 Artikel suchen")],
                "en": [("start", "ℹ️ Help and instructions"), ("filter", "🔧 Set up filter"),
                       ("filter_clear", "❌ Clear filter"), ("find", "🔎 Find a deal")],
                "ru": [("start", "ℹ️ Помощь и инструкция"), ("filter", "🔧 Настроить фильтр"),
                       ("filter_clear", "❌ Убрать фильтр"), ("find", "🔎 Найти товар")],
            }
            cmds = [BotCommand(c, d) for c, d in CMDS[lang]]
            try:
                await context.bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=uid))
                log.info(f"[lang] uid={uid} → {lang}, команды обновлены")
            except Exception as e:
                log.warning(f"[lang] set_my_commands failed: {e}")
            labels = {"de": "DE", "en": "EN", "ru": "RU"}
            await query.answer(f"{labels[lang]} gesetzt / set / выбран")
        # Перерисовываем меню чтобы показать ✓ у выбранного языка
        text, keyboard = build_filter_menu(uid, uf)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            pass
        return

    elif data.startswith("fcat:"):
        key = data.split("fcat:", 1)[1]
        cat = FILTER_CATEGORIES.get(key)
        if cat:
            display, keywords = cat
            merged = list(dict.fromkeys(uf.get(uid) + keywords))
            uf.set(uid, merged)
            await query.answer(f"✅ {display}")
            log.info(f"[filter] uid={uid} fcat={key} → {uf.get(uid)}")
        else:
            log.warning(f"[filter] unknown fcat key: {repr(key)}")
            await query.answer()
            return
    else:
        await query.answer()
        return

    text, keyboard = build_filter_menu(uid, uf)
    # Всегда удаляем старое и шлём новое — самый надёжный способ
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard
    )

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автоматически сохраняем новые посты из канала для поиска."""
    msg = update.channel_post
    if not msg: return
    text = msg.text or msg.caption or ""
    if not text.strip(): return
    store: PostStore = context.bot_data["store"]
    store.add(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        date_unix=int(msg.date.timestamp()),
        text=text,
    )
    log.info(f"Сохранён пост #{msg.message_id} для поиска")

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice: return
    if not WHISPER_AVAILABLE or context.bot_data.get("whisper") is None:
        await update.message.reply_text(
            "Голосовой поиск недоступен.\nУстановите: pip install faster-whisper",
            parse_mode="HTML"
        )
        return
    store: PostStore = context.bot_data["store"]
    if not store.posts:
        await update.message.reply_text("Пока нет данных для поиска.")
        return
    await update.message.reply_text("🎤 Распознаю…")
    voice   = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)
    whisper = context.bot_data["whisper"]
    with tempfile.TemporaryDirectory() as tmpdir:
        ogg = os.path.join(tmpdir, "voice.ogg")
        await tg_file.download_to_drive(ogg)
        # language=None — Whisper сам определяет язык (русский или немецкий)
        segments, info = whisper.transcribe(ogg, language=None)
        text = " ".join(s.text.strip() for s in segments).strip()
        log.info(f"Распознан язык: {info.language} (вероятность {info.language_probability:.0%})")
    if not text:
        await update.message.reply_text("Не смог распознать 😕")
        return
    log.info(f"Голос распознан: «{text}»")
    ch = context.bot_data["channel_username"]
    await reply_with_best(update, context, text, store, ch)

# ─────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────

async def post_init(app: Application):
    """Запускаем RSS-цикл как фоновую задачу после старта Application."""
    creds = app.bot_data["creds"]
    store = app.bot_data["store"]
    me    = await app.bot.get_me()
    app.bot_data["bot_username"] = me.username

    # Получаем channel_username из bot_data
    channel_username = app.bot_data.get("channel_username", "")

    # Устанавливаем команды для каждого языка
    commands = {
        "de": [
            BotCommand("start",        "ℹ️ Hilfe und Anleitung"),
            BotCommand("filter",       "🔧 Filter einrichten"),
            BotCommand("filter_clear", "❌ Filter löschen"),
            BotCommand("find",         "🔎 Artikel suchen"),
        ],
        "en": [
            BotCommand("start",        "ℹ️ Help and instructions"),
            BotCommand("filter",       "🔧 Set up filter"),
            BotCommand("filter_clear", "❌ Clear filter"),
            BotCommand("find",         "🔎 Find a deal"),
        ],
        "ru": [
            BotCommand("start",        "ℹ️ Помощь и инструкция"),
            BotCommand("filter",       "🔧 Настроить фильтр товаров"),
            BotCommand("filter_clear", "❌ Убрать фильтр"),
            BotCommand("find",         "🔎 Найти товар"),
        ],
    }
    for lang, cmds in commands.items():
        await app.bot.set_my_commands(cmds, language_code=lang)
    # Дефолт (без language_code) — немецкий, т.к. основная аудитория
    await app.bot.set_my_commands(commands["de"])

#    asyncio.ensure_future(rss_loop(app.bot, creds["CHAT_ID"], store, me.username, app.bot_data["user_filters"]))
    async def rss_loop_resilient(*args, **kwargs):
        """Перезапускает rss_loop если он упал — бесконечно."""
        while True:
            try:
                await rss_loop(*args, **kwargs)
            except Exception as e:
                log.error(f"⚠️ rss_loop упал: {e}. Перезапуск через 60 сек...")
                await asyncio.sleep(60)

    asyncio.ensure_future(rss_loop_resilient(app.bot, creds["CHAT_ID"], store, me.username, app.bot_data["user_filters"], channel_username))
    log.info(f"▶ RSS-цикл запущен в фоне (@{me.username})")

def main():
    creds   = load_credentials()
    store        = PostStore(CONFIG["POSTS_FILE"])
    user_filters = UserFilters(USER_FILTERS_FILE)
    whisper = None  # голосовой поиск отключён
    channel_username = creds.get("CHANNEL_USERNAME", "").lstrip("@")

    app = (Application.builder()
           .token(creds["BOT_TOKEN"])
           .connect_timeout(30)
           .read_timeout(30)
           .write_timeout(30)
           .pool_timeout(30)
           .post_init(post_init)
           .build())

    app.bot_data.update({
        "creds":            creds,
        "store":            store,
        "whisper":          whisper,
        "channel_username": channel_username,
        "user_filters":     user_filters,
    })

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("find",         cmd_find))
    app.add_handler(CommandHandler("filter",       cmd_filter))
    app.add_handler(CommandHandler("filter_clear", cmd_filter_clear))
    app.add_handler(CallbackQueryHandler(on_filter_callback, pattern="^(filter_|fcat:|fdel:|fdone|fadd|noop|flang:)"))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Глобальный обработчик ошибок — логирует и продолжает работу
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        err_str = str(err)
        # Сетевые ошибки — не критично, polling восстановится сам
        if any(x in err_str for x in ["ReadError", "NetworkError", "ConnectError",
                                        "TimedOut", "getaddrinfo", "ConnectionError"]):
            log.warning(f"⚠️ Сетевая ошибка (восстановление автоматическое): {type(err).__name__}")
        else:
            log.error(f"⚠️ Ошибка в боте: {err}", exc_info=err)

    app.add_error_handler(error_handler)

    log.info(f"▶ Запуск mydealz бота (@{channel_username or '?'})")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    import time as _time
    import asyncio as _asyncio

    restart_delay = 10
    while True:
        try:
            # Создаём свежий event loop при каждом запуске
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            try:
                main()
            finally:
                # Корректно закрываем loop чтобы не было утечек
                try:
                    pending = _asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(_asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                loop.close()
        except Exception as e:
            err = str(e)
            if any(x in err for x in ["Timed out", "TimedOut", "NetworkError",
                                        "ConnectionError", "Event loop is closed"]):
                log.warning(f"⚠️ Сетевая ошибка: {err}. Перезапуск через {restart_delay} сек...")
            else:
                log.error(f"⚠️ Бот упал: {err}. Перезапуск через {restart_delay} сек...")
            _time.sleep(restart_delay)
