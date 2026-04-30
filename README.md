# Telegram Deals Bot
Telegram bot for collecting deals from mydealz RSS feed and publishing them to a Telegram channel.
Live Telegram Channel: https://t.me/dealcheckde

## Features
- Parses mydealz RSS feed
- Publishes new deals to Telegram channel
- Extracts prices, discounts and product images
- Supports user filters by keywords
- Allows users to search recent deals
- Supports German, English and Russian interface
- Stores recent posts locally for search
- Includes error handling and automatic restart logic

## Technologies
- Python
- Telegram Bot API
- RSS / XML parsing
- BeautifulSoup
- BM25 search
- RapidFuzz
- JSON storage

## How to run
```bash
pip install -r requirements.txt
python mydealz_bot.py

Create a config file:config_mydealz.json. Then add your Telegram bot token and channel ID.
