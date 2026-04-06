#!/bin/bash
# Run this ONCE on the Oracle Cloud VM:
#   curl -sL <SCRIPT_URL> | bash
# or copy-paste the commands below.

set -e

echo "=== Oracle VM Setup for Daily Digest ==="

# 1. System packages
echo "--- Installing packages ---"
sudo apt update && sudo apt install -y python3 python3-pip python3-venv cron

# 2. Create app directory and copy bot files
APP_DIR="/opt/daily-digest"
sudo mkdir -p "$APP_DIR"
sudo chown "$(whoami)":"$(whoami)" "$APP_DIR"

# Create virtual environment
python3 -m venv "$APP_DIR/.venv"
source "$APP_DIR/.venv/bin/activate"
pip install --upgrade pip
pip install requests

# 3. Write the bot
cat > "$APP_DIR/bot.py" << 'PYTHON_EOF'
#!/usr/bin/env python3
"""Daily article digest bot — simple fetch + send to Telegram."""

import os
import json
import logging
import requests
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT   = os.environ["TELEGRAM_CHAT_ID"]
NEWS_API_KEY    = os.environ["NEWS_API_KEY"]
OPENROUTER_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
LLM_MODEL       = os.environ.get("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

CATEGORIES = [
    {"label": "📊 Οικονομία & Επιχειρήσεις", "query": "economy OR inflation OR stock market OR GDP OR crypto OR business OR finance", "count": 5},
    {"label": "🔬 Επιστήμη & Τεχνολογία",    "query": "science OR research OR technology OR discovery OR space OR medicine",        "count": 3},
    {"label": "🌍 Κόσμος & Πολιτισμός",        "query": "geography OR culture OR travel OR society OR international",               "count": 2},
]

NEWS_API_URL = "https://newsapi.org/v2/everything"
OLROU_URL    = "https://openrouter.ai/api/v1/chat/completions"


def fetch_articles(query: str, max_results: int) -> list[dict]:
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": max_results * 3,
        "from": (datetime.utcnow()).strftime("%Y-%m-%d"),
        "apiKey": NEWS_API_KEY,
    }
    resp = requests.get(NEWS_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        logging.error(f"NewsAPI error: {data.get('message', data)}")
        return []
    seen = set()
    articles = []
    for a in data.get("articles", []):
        if not a.get("url") or a["url"] in seen or not a.get("title"):
            continue
        seen.add(a["url"])
        articles.append(a)
    return articles


def fetch_all() -> list[dict]:
    result = []
    for cat in CATEGORIES:
        logging.info(f"Fetching {cat['label']} …")
        for a in fetch_articles(cat["query"], cat["count"]):
            a["_label"] = cat["label"]
            result.append(a)
    logging.info(f"Fetched {len(result)} total articles")
    return result


def curate_with_llm(articles: list[dict]) -> str:
    payload = [
        {
            "role": "system",
            "content": (
                "You are a news-editor. I'll give you a list of articles with categories. "
                "Pick the MOST interesting/recent articles: exactly 10 total (5 economics, 3 science, 2 world). "
                "Return ONLY formatted text (no JSON, no markdown code blocks). "
                "For each article: title, one-sentence summary, source, url. "
                "Use the category label provided before each group."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                [
                    {
                        "title": a["title"],
                        "description": a.get("description", ""),
                        "url": a["url"],
                        "source": a.get("source", {}).get("name", "Unknown"),
                        "category": a["_label"],
                    }
                    for a in articles
                ]
            ),
        },
    ]
    logging.info("Asking LLM to curate …")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/daily-digest",
    }
    resp = requests.post(
        OLROU_URL,
        headers=headers,
        json={"model": LLM_MODEL, "messages": payload, "temperature": 0.3},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def curate_rules(articles: list[dict]) -> str:
    from collections import defaultdict
    groups = defaultdict(list)
    for a in articles:
        groups[a["_label"]].append(a)
    lines = []
    targets = {c["label"]: c["count"] for c in CATEGORIES}
    for label, art_list in groups.items():
        lines.append(f"\n{label}")
        for i, a in enumerate(art_list[: targets.get(label, 3)]):
            lines.append(f"  {i+1}. {a['title']}")
            if a.get("description"):
                lines.append(f"     {a['description']}")
            lines.append(f"     🔗 {a['url']}")
    return "\n".join(lines)


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(text) > 4000:
        text = text[:4000] + "\n\n…(truncated)"
    resp = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT,
            "text": text,
            "parse_mode": "Markdown",
        },
        timeout=30,
    )
    resp.raise_for_status()
    logging.info("Digest sent to Telegram ✓")


def main():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    logging.info(f"=== Daily Digest {today} ===")

    articles = fetch_all()
    if not articles:
        send_telegram(f"☀️ Daily Digest ({today})\n\nNo articles found today.")
        return

    if OPENROUTER_KEY:
        try:
            digest = curate_with_llm(articles)
        except Exception:
            logging.exception("LLM failed, using fallback")
            digest = curate_rules(articles)
    else:
        digest = curate_rules(articles)

    header = f"☀️ Καλημέρα! Daily digest — {today}\n\n"
    send_telegram(header + digest)


if __name__ == "__main__":
    main()
PYTHON_EOF

# 4. Write the .env file (with values you'll fill in)
cat > "$APP_DIR/.env" << ENV_EOF
# Fill these in with your actual keys:
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_CHAT_ID
NEWS_API_KEY=YOUR_NEWS_KEY
OPENROUTER_API_KEY=YOUR_OPENROUTER_KEY
ENV_EOF

# 5. Set up cron job — runs at 8 AM Greece time (06:00 UTC winter / 05:00 UTC summer)
# We run at 05:00 UTC which is 8 AM EET (winter) / 7 AM EEST (summer)
# Better: use Europe/Athens timezone in cron
(crontab -l 2>/dev/null | grep -v "daily-digest"; \
 echo '0 5 * * * cd /opt/daily-digest && .venv/bin/python3 bot.py >> /opt/daily-digest/cron.log 2>&1') | crontab -

sudo systemctl enable --now cron

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "1. Edit the .env file:  nano /opt/daily-digest/.env"
echo "2. Fill in your Telegram bot token, chat ID, and API keys"
echo "3. Test it:  cd /opt/daily-digest && .venv/bin/python3 bot.py"
echo "4. Check the cron:  crontab -l"
