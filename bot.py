#!/usr/bin/env python3
"""Daily article digest bot.

Fetches news (GNews), scientific papers (PubMed), curates with an LLM,
and sends to Telegram via a scheduled GitHub Action.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    force=True,
)

TELEGRAM_TOKEN = "8667775748:AAGxezFo2AlnTwMijbaJxT-2l61XqbgfXY0"
TELEGRAM_CHAT  = "5956713175"
GNEWS_API_KEY  = "2c4ae04520de960086ad6a7b07188356"
PUBMED_KEY     = "05abc7658eeac8454076a773b5368fff6c09"
OPENROUTER_KEY = "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"
LLM_MODEL      = "meta-llama/llama-3.3-70b-instruct:free"
PUBMED_COUNT   = 2

NEWS_CATEGORIES = [
    {"label": "📊 Οικονομία & Επιχειρήσεις", "topic": "business",     "country": "us", "count": 5},
    {"label": "🔬 Επιστήμη & Τεχνολογία",    "topic": "technology",  "country": "us", "count": 3},
    {"label": "🌍 Κόσμος & Πολιτισμός",      "topic": "world",       "country": "us", "count": 2},
]

GNEWS_URL      = "https://gnews.io/api/v4/top-headlines"
GNEWS_SEARCH   = "https://gnews.io/api/v4/search"
PUBMED_SEARCH  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def fetch_gnews(category: str, country: str, count: int) -> list[dict]:
    resp = requests.get(GNEWS_URL, params={
        "category": category,
        "country":  country,
        "lang":     "en",
        "max":      count * 3,
        "token":    GNEWS_API_KEY,
    }, timeout=30)
    resp.raise_for_status()
    articles = []
    seen = set()
    for a in resp.json().get("articles", []):
        if not a.get("url") or a["url"] in seen or not a.get("title"):
            continue
        seen.add(a["url"])
        articles.append(a)
    return articles[: count * 2]


def fetch_all_news() -> list[dict]:
    result = []
    for cat in NEWS_CATEGORIES:
        logging.info(f"Fetching {cat['label']} …")
        for a in fetch_gnews(cat["topic"], cat["country"], cat["count"]):
            a["_label"] = cat["label"]
            result.append(a)
    logging.info(f"Fetched {len(result)} news articles")
    return result


def fetch_pubmed(count: int) -> list[dict]:
    resp = requests.get(PUBMED_SEARCH, params={
        "db": "pubmed",
        "term": "last 7 days[edat]",
        "sort": "relevance",
        "retmax": count * 5,
        "retmode": "json",
        "api_key": PUBMED_KEY,
    }, timeout=30)
    resp.raise_for_status()
    ids = resp.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    resp = requests.get(PUBMED_SUMMARY, params={
        "db":     "pubmed",
        "id":     ",".join(ids),
        "retmode":"json",
        "api_key": PUBMED_KEY,
    }, timeout=30)
    resp.raise_for_status()
    docs = resp.json().get("result", {})

    papers = []
    for uid, doc in docs.items():
        if uid == "uids":
            continue
        papers.append({
            "title":     doc.get("title", ""),
            "description": "",
            "url":       f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "source":    doc.get("source", ""),
            "category":  "pubmed",
            "_label":    "🧬 Επιστημονικές Δημοσιεύσεις (PubMed)",
        })

    return papers[:count]


def curate_with_llm(news: list[dict], papers: list[dict]) -> str:
    items = news + papers
    payload = [
        {
            "role": "system",
            "content": (
                "You are a news-editor. Given articles and scientific papers, curate them. "
                "News sections: exactly 5 economics, 3 science/tech, 2 world. "
                "Papers: include all. "
                "Return ONLY formatted text — NO JSON, NO markdown code blocks, NO backticks. "
                "For each item: short title, one-sentence summary, source, url. "
                "Use the _label field as section header exactly as given. "
                "Use Unicode emoji only (no :emoji: syntax)."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                [{"title": a["title"], "description": a.get("description",""),
                  "url": a["url"], "source": a.get("source","Unknown"),
                  "_label": a["_label"]}
                 for a in items],
                ensure_ascii=False,
            ),
        },
    ]
    logging.info("Asking LLM to curate …")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type":   "application/json",
            "HTTP-Referer":   "https://github.com/daily-digest",
        },
        json={"model": LLM_MODEL, "messages": payload, "temperature": 0.3},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def curate_rules(news: list[dict], papers: list[dict]) -> str:
    from collections import defaultdict
    groups = defaultdict(list)
    for a in news + papers:
        groups[a["_label"]].append(a)
    lines = []
    targets = {c["label"]: c["count"] for c in NEWS_CATEGORIES}
    targets["🧬 Επιστημονικές Δημοσιεύσεις (PubMed)"] = PUBMED_COUNT
    for label, alist in groups.items():
        lines.append(f"\n{label}")
        for i, a in enumerate(alist[: targets.get(label, 5)]):
            lines.append(f"  {i+1}. {a['title']}")
            if a.get("description"):
                lines.append(f"     {a['description']}")
            lines.append(f"     🔗 {a['url']}")
    return "\n".join(lines)


def send_telegram(text: str):
    MAX = 4000
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    header = f"☀️ Καλημέρα! Daily digest — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
    full = header + text

    # Send in chunks if needed (no markdown to avoid escaping issues)
    for start in range(0, len(full), MAX):
        chunk = full[start:start + MAX]
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=30)
        resp.raise_for_status()
    logging.info(f"Digest sent to Telegram ✓")


def main():
    logging.info(f"=== Daily Digest {datetime.now(timezone.utc).strftime('%Y-%m-%d')} ===")

    news   = fetch_all_news()
    papers = []
    try:
        papers = fetch_pubmed(PUBMED_COUNT)
        logging.info(f"Fetched {len(papers)} PubMed papers")
    except Exception:
        logging.exception("PubMed fetch failed")

    all_items = news + papers
    if not all_items:
        send_telegram("No articles found today.")
        return

    open_key = OPENROUTER_KEY.strip() if OPENROUTER_KEY else ""
    if open_key and len(open_key) > 10:
        try:
            digest = curate_with_llm(news, papers)
        except Exception:
            logging.exception("LLM failed, using fallback")
            digest = curate_rules(news, papers)
    else:
        digest = curate_rules(news, papers)

    send_telegram(digest)


if __name__ == "__main__":
    main()
