#!/usr/bin/env python3
"""Daily article digest bot.

Fetches news via GNews (server-side compatible), curates with an LLM,
and sends to Telegram. Designed to run as a one-shot script (e.g. GitHub Actions).
"""

import os
import json
import logging
import requests
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    force=True,
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
GNEWS_API_KEY  = os.environ["GNEWS_API_KEY"]
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
LLM_MODEL      = os.environ.get("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

CATEGORIES = [
    {"label": "📊 Οικονομία & Επιχειρήσεις", "query": "economy inflation stock market GDP crypto business finance", "count": 5},
    {"label": "🔬 Επιστήμη & Τεχνολογία",    "query": "science research technology discovery space medicine",        "count": 3},
    {"label": "🌍 Κόσμος & Πολιτισμός",        "query": "geography culture travel society international",               "count": 2},
]

GNEWS_URL = "https://gnews.io/api/v4/search"


# ── fetching ────────────────────────────────────────────────────────────────
def fetch_articles(query: str, max_results: int) -> list[dict]:
    params = {
        "q": query,
        "lang": "en",
        "sortby": "publishedAt",
        "max": 15,
        "token": GNEWS_API_KEY,
    }
    resp = requests.get(GNEWS_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    articles = []
    seen = set()
    for a in data.get("articles", []):
        if not a.get("url") or a["url"] in seen or not a.get("title"):
            continue
        seen.add(a["url"])
        articles.append(a)
    return articles[: max_results * 2]


def fetch_all() -> list[dict]:
    result = []
    for cat in CATEGORIES:
        logging.info(f"Fetching {cat['label']} …")
        for a in fetch_articles(cat["query"], cat["count"]):
            a["_label"] = cat["label"]
            result.append(a)
    logging.info(f"Fetched {len(result)} total articles")
    return result


# ── LLM curation ────────────────────────────────────────────────────────────
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
        "https://openrouter.ai/api/v1/chat/completions",
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


# ── delivery ────────────────────────────────────────────────────────────────
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


# ── main ────────────────────────────────────────────────────────────────────
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
