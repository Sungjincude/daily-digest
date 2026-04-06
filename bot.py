#!/usr/bin/env python3
"""Daily article digest bot.

Fetches news (GNews) and scientific papers (PubMed), curates with an LLM,
and sends to Telegram via a scheduled GitHub Action.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta

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
LLM_MODEL      = "mistralai/mistral-small-3.1-24b-instruct:free"

NEWS_CATEGORIES = [
    {"label": "BUSINESS & ECONOMY",       "query": "economy inflation stock market GDP crypto business finance", "count": 5},
    {"label": "SCIENCE & TECHNOLOGY",     "query": "science research technology discovery space medicine",       "count": 3},
    {"label": "WORLD & CULTURE",          "query": "geography culture travel society international",             "count": 2},
]

GNEWS_SEARCH_URL = "https://gnews.io/api/v4/search"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def fetch_gnews(query: str, count: int) -> list[dict]:
    resp = requests.get(GNEWS_SEARCH_URL, params={
        "q": query, "lang": "en", "sortby": "publishedAt",
        "max": count * 3, "token": GNEWS_API_KEY,
    }, timeout=30)
    resp.raise_for_status()
    seen, articles = set(), []
    for a in resp.json().get("articles", []):
        if a.get("url") and a["url"] not in seen and a.get("title"):
            seen.add(a["url"])
            articles.append(a)
    return articles[: count * 2]


def fetch_all_news() -> list[dict]:
    result = []
    for cat in NEWS_CATEGORIES:
        logging.info(f"Fetching {cat['label']} ...")
        for a in fetch_gnews(cat["query"], cat["count"]):
            a["_label"] = cat["label"]
            result.append(a)
    logging.info(f"Fetched {len(result)} news articles")
    return result


def fetch_pubmed(count: int) -> list[dict]:
    logging.info("Fetching PubMed papers ...")
    try:
        resp = requests.get(PUBMED_SEARCH, params={
            "db": "pubmed", "term": "last 7 days[edat]",
            "sort": "relevance", "retmax": count*5,
            "retmode": "json", "api_key": PUBMED_KEY,
        }, timeout=30)
        resp.raise_for_status()
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        resp = requests.get(PUBMED_SUMMARY, params={
            "db": "pubmed", "id": ",".join(ids[:count*3]),
            "retmode": "json", "api_key": PUBMED_KEY,
        }, timeout=30)
        resp.raise_for_status()
        docs = resp.json().get("result", {})
        papers = []
        for uid, doc in docs.items():
            if uid == "uids":
                continue
            title = doc.get("title", "").strip()
            source = doc.get("source", "").strip()
            papers.append({
                "title": title, "description": "",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                "source": source or "PubMed", "_label": "SCIENTIFIC PAPERS",
            })
        return papers[:count]
    except Exception as e:
        logging.warning(f"PubMed fetch failed: {e}")
        return []


def curate_with_llm(news: list[dict], papers: list[dict]) -> str:
    all_items = news + papers
    logging.info(f"Sending {len(all_items)} items to LLM for curation ...")

    prompt = (
        "You are a news digest curator. Below is a JSON array of news articles "
        "and scientific papers. Curate exactly 10 items: 5 economics, 3 science, "
        "2 world. Include all scientific papers as a separate section.\n"
        "Format: use section headers in ALL CAPS, then for each item:\n"
        "NUMBER. TITLE\n"
        "One-sentence summary.\n"
        "URL\n\n"
        "Keep it clean and readable. No markdown code blocks, no JSON output.\n\n"
        + json.dumps(
            [{"title": a["title"], "description": a.get("description",""),
              "url": a["url"], "source": a.get("source","Unknown"),
              "category": a["_label"]}
             for a in all_items],
            ensure_ascii=False,
        )
    )

    resp = requests.post(OPENROUTER_URL, json={
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }, headers={
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/daily-digest",
    }, timeout=120)

    if not resp.ok:
        logging.warning(f"LLM returned {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()

    return resp.json()["choices"][0]["message"]["content"]


def format_fallback(news: list[dict], papers: list[dict]) -> str:
    """Non-LLM fallback formatting."""
    groups = defaultdict(list)
    for a in news:
        groups[a["_label"]].append(a)
    lines = []
    for cat in NEWS_CATEGORIES:
        label, articles = cat["label"], groups.get(cat["label"], [])[:cat["count"]]
        if articles:
            lines.append(f"\n--- {label} ---")
            for i, a in enumerate(articles, 1):
                lines.append(f"  {i}. {a['title']}")
                desc = a.get("description","")
                if desc:
                    lines.append(f"     {desc[:150]}")
                lines.append(f"     {a['url']}")
    if papers:
        lines.append(f"\n--- SCIENTIFIC PAPERS ---")
        for i, p in enumerate(papers, 1):
            lines.append(f"  {i}. {p['title']}")
            lines.append(f"     {p['url']}")
    return "\n".join(lines)


def send_telegram(text: str):
    """Send plain text to Telegram (no markdown parsing to avoid escaping issues)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    full = f"Good morning! Daily digest - {today}\n\n{text}"
    MAX = 4000
    for start in range(0, len(full), MAX):
        chunk = full[start:start + MAX]
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT, "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=30)
        resp.raise_for_status()
    logging.info(f"Digest sent to Telegram")


def main():
    logging.info("=== Daily Digest ===")
    news = fetch_all_news()
    papers = fetch_pubmed(2)
    if not news and not papers:
        send_telegram("No articles found today.")
        return
    # Try LLM curation, fallback to rules
    try:
        digest = curate_with_llm(news, papers)
        logging.info("LLM curation succeeded")
    except Exception as e:
        logging.warning(f"LLM failed ({e}), using fallback")
        digest = format_fallback(news, papers)
    send_telegram(digest)
    logging.info("Done!")


if __name__ == "__main__":
    main()
