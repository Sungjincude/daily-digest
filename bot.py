#!/usr/bin/env python3
"""Daily article digest bot.

Fetches news (GNews), scientific papers (PubMed), curates with an LLM,
and sends to Telegram via a scheduled GitHub Action.
"""

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime

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
PUBMED_COUNT   = 2  # number of scientific papers per day

NEWS_CATEGORIES = [
    {"label": "📊 Οικονομία & Επιχειρήσεις", "query": "economy inflation stock market GDP crypto business finance", "count": 5},
    {"label": "🔬 Επιστήμη & Τεχνολογία",    "query": "science research technology discovery space medicine",        "count": 3},
    {"label": "🌍 Κόσμος & Πολιτισμός",       "query": "geography culture travel society international",            "count": 2},
]

GNEWS_URL     = "https://gnews.io/api/v4/search"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


# ── GNews fetching ──────────────────────────────────────────────────────────
def fetch_gnews(query: str, count: int) -> list[dict]:
    resp = requests.get(GNEWS_URL, params={
        "q": query, "lang": "en", "sortby": "publishedAt",
        "max": count * 2, "token": GNEWS_API_KEY,
    }, timeout=30)
    resp.raise_for_status()
    seen = set()
    articles = []
    for a in resp.json().get("articles", []):
        if not a.get("url") or a["url"] in seen or not a.get("title"):
            continue
        seen.add(a["url"])
        articles.append(a)
    return articles


def fetch_all_news() -> list[dict]:
    result = []
    for cat in NEWS_CATEGORIES:
        logging.info(f"Fetching {cat['label']} …")
        for a in fetch_gnews(cat["query"], cat["count"]):
            a["_label"] = cat["label"]
            result.append(a)
    logging.info(f"Fetched {len(result)} news articles")
    return result


# ── PubMed fetching ─────────────────────────────────────────────────────────
def fetch_pubmed(count: int) -> list[dict]:
    """Fetch recent notable scientific papers from PubMed."""
    today = datetime.utcnow().strftime("%Y/%m/%d")
    yesterday = (datetime.utcnow() - __import__('datetime').timedelta(days=3)).strftime("%Y/%m/%d")

    # Step 1: search for PMIDs of recent papers
    resp = requests.get(PUBMED_SEARCH, params={
        "db": "pubmed",
        "term": f"last 7 days[edat]",
        "sort": "relevance",
        "retmax": count * 3,
        "retmode": "json",
        "api_key": PUBMED_KEY,
    }, timeout=30)
    resp.raise_for_status()
    ids = resp.json().get("esearchresult", {}).get("idlist", [])

    if not ids:
        return []

    # Step 2: get summaries for those PMIDs
    resp = requests.get(PUBMED_SUMMARY, params={
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "json",
        "api_key": PUBMED_KEY,
    }, timeout=30)
    resp.raise_for_status()
    docs = resp.json().get("result", {})

    papers = []
    for uid, doc in docs.items():
        if uid == "uids":
            continue
        title = doc.get("title", "")
        source = doc.get("source", "")
        pubdate = doc.get("pubdate", "")
        doi = doc.get("articleids", [{}])[-1].get("value", "")
        url = f"https://pubmed.ncbi.nlm.nih.gov/{uid}/"
        papers.append({
            "title": title,
            "description": "",
            "url": url,
            "source": source,
            "publishedAt": pubdate,
            "category": "pubmed",
            "_label": "🧬 Επιστημονικές Δημοσιεύσεις (PubMed)",
        })

    return papers[:count]


# ── LLM curation ────────────────────────────────────────────────────────────
def curate_with_llm(news: list[dict], papers: list[dict]) -> str:
    items = news + papers
    payload = [
        {
            "role": "system",
            "content": (
                "You are a news-editor. I'll give you articles and scientific papers. "
                "Curate into sections keeping the provided category labels. "
                "For news: pick 5 economics, 3 science/tech, 2 world. "
                "For papers: include all. "
                "Return ONLY formatted text (no JSON, no markdown code blocks, no backticks). "
                "For each item: short title, one-sentence summary, source, url. "
                "Use the _label field provided as section headers."
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
                        "source": a.get("source", "Unknown"),
                        "_label": a["_label"],
                    }
                    for a in items
                ],
                ensure_ascii=False,
            ),
        },
    ]
    logging.info("Asking LLM to curate …")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/daily-digest",
        },
        json={"model": LLM_MODEL, "messages": payload, "temperature": 0.3},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def curate_rules(news: list[dict], papers: list[dict]) -> str:
    """Fallback without LLM."""
    from collections import defaultdict
    groups = defaultdict(list)
    for a in news:
        groups[a["_label"]].append(a)
    for a in papers:
        groups[a["_label"]].append(a)

    lines = []
    targets = {c["label"]: c["count"] for c in NEWS_CATEGORIES}
    targets["🧬 Επιστημονικές Δημοσιεύσεις (PubMed)"] = PUBMED_COUNT
    for label, art_list in groups.items():
        lines.append(f"\n{label}")
        for i, a in enumerate(art_list[: targets.get(label, 5)]):
            lines.append(f"  {i+1}. {a['title']}")
            if a.get("description"):
                lines.append(f"     {a['description']}")
            lines.append(f"     🔗 {a['url']}")
    return "\n".join(lines)


# ── delivery ────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    """Send digest in chunks (Telegram msg limit is 4096 chars)."""
    MAX = 4000
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Send first chunk with header
    header = f"☀️ Καλημέρα! Daily digest — {datetime.utcnow().strftime('%Y-%m-%d')}\n\n"
    full = header + text
    # If fits in one message
    if len(full) <= MAX:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": full}, timeout=30)
        resp.raise_for_status()
        return
    # Otherwise split into chunks
    for start in range(0, len(full), MAX):
        chunk = full[start:start + MAX]
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": chunk}, timeout=30)
        resp.raise_for_status()
    logging.info(f"Digest sent to Telegram in {len(full) // MAX + 1} chunks ✓")


# ── main ────────────────────────────────────────────────────────────────────
def main():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    logging.info(f"=== Daily Digest {today} ===")

    news = fetch_all_news()
    papers = []
    try:
        papers = fetch_pubmed(PUBMED_COUNT)
        logging.info(f"Fetched {len(papers)} PubMed papers")
    except Exception:
        logging.exception("PubMed fetch failed")

    all_items = news + papers
    if not all_items:
        send_telegram(f"☀️ Daily Digest ({today})\n\nNo articles found today.")
        return

    if OPENROUTER_KEY:
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
