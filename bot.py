#!/usr/bin/env python3
"""Daily article digest bot.

Fetches news via NewsAPI, curates with an LLM, and sends to Telegram.
Designed to run as a one-shot script (e.g. GitHub Actions).

Uses OpenRouter (openrouter.ai) free-tier models by default.
"""

import os
import json
import logging
import requests
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ── config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT   = os.environ["TELEGRAM_CHAT_ID"]
NEWS_API_KEY    = os.environ["NEWS_API_KEY"]
OPENROUTER_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
LLM_MODEL       = os.environ.get("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
# ── article targets ────────────────────────────────────────────────────────
CATEGORIES = [
    {"label": "📊 Οικονομία & Επιχειρήσεις", "query": "economy OR inflation OR stock market OR GDP OR crypto OR business OR finance", "count": 5},
    {"label": "🔬 Επιστήμη & Τεχνολογία",    "query": "science OR research OR technology OR discovery OR space OR medicine",        "count": 3},
    {"label": "🌍 Κόσμος & Πολιτισμός",        "query": "geography OR culture OR travel OR society OR international",               "count": 2},
]

NEWS_API_URL = "https://newsapi.org/v2/everything"
OLROU_URL    = "https://openrouter.ai/api/v1/chat/completions"


# ── fetching ────────────────────────────────────────────────────────────────
def fetch_articles(query: str, max_results: int) -> list[dict]:
    """Return up to max_results * 2 articles for later culling."""
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": max_results * 2,
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
    """Fetch across categories, tagging each article."""
    result = []
    for cat in CATEGORIES:
        logging.info(f"Fetching {cat['label']} …")
        for a in fetch_articles(cat["query"], cat["count"]):
            a["_label"] = cat["label"]
            result.append(a)
    logging.info(f"Fetched {len(result)} total articles")
    return result


# ── LLM curation ────────────────────────────────────────────────────────────
def curate(articles: list[dict]) -> str:
    """Ask an LLM to pick the best 10 and format a digest."""
    payload = [
        {
            "role": "system",
            "content": (
                "You are a news-editor. I'll give you a list of articles with categories. "
                "Pick the MOST interesting/recent articles: exactly 10 total, trying to keep "
                "5 economics/business, 3 science/tech, 2 world/culture. "
                "Return ONLY formatted text (no markdown blocks, no JSON). "
                "For each article: title (short), one-sentence summary, source, url. "
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
                        "publishedAt": a.get("publishedAt", ""),
                        "_label": a["_label"],
                    }
                    for a in articles
                ],
                ensure_ascii=False,
            ),
        },
    ]
    logging.info("Asking LLM to curate …")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/user/daily-digest",
    }
    resp = requests.post(
        OLROU_URL,
        headers=headers,
        json={"model": LLM_MODEL, "messages": payload, "temperature": 0.3},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def curate_rules_fallback(articles: list[dict]) -> str:
    """Rule-based fallback curation when LLM is unavailable."""
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
    """Send a message (≤ 4096 chars) to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(text) > 4000:
        text = text[:4000] + "\n\n…(truncated)"
    resp = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
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

    try:
        if OPENROUTER_KEY:
            digest = curate(articles)
        else:
            logging.warning("No LLM key — using rule-based fallback")
            digest = curate_rules_fallback(articles)
    except Exception:
        logging.exception("LLM failed, falling back to rules")
        digest = curate_rules_fallback(articles)

    header = f"☀️ Καλημέρα! Daily digest — {today}\n\n"
    send_telegram(header + digest)


if __name__ == "__main__":
    main()
