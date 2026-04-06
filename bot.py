#!/usr/bin/env python3
"""Daily article digest bot.

Fetches news from multiple sources (GNews + RSS fallback),
PubMed papers, curates with an LLM, sends to Telegram.
"""

import json
import logging
import re
import xml.etree.ElementTree as ET
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
LLM_MODEL      = "stepfun/step-3.5-flash:free"

NEWS_CATEGORIES = [
    {"label": "BUSINESS AND ECONOMY",  "query": "economy stock market business finance", "count": 5},
    {"label": "SCIENCE AND TECHNOLOGY","query": "science technology research AI",         "count": 3},
    {"label": "WORLD AND CULTURE",     "query": "world culture international",            "count": 2},
]

GNEWS_SEARCH  = "https://gnews.io/api/v4/search"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

# RSS feeds as reliable free fallback
RSS_FEEDS = {
    "BUSINESS AND ECONOMY":   ["https://www.google.com/rss", "https://feeds.bbci.co.uk/news/business/rss.xml"],
    "SCIENCE AND TECHNOLOGY": ["https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "https://feeds.bbci.co.uk/news/technology/rss.xml"],
    "WORLD AND CULTURE":      ["https://feeds.bbci.co.uk/news/world/rss.xml"],
}


def fetch_from_rss(label: str, count: int) -> list[dict]:
    """Fetch articles from RSS feeds."""
    feeds = RSS_FEEDS.get(label, [])
    articles = []
    seen = set()
    for feed_url in feeds:
        try:
            r = requests.get(feed_url, timeout=20)
            if not r.ok:
                continue
            text = r.text.replace("]]>", "")
            root = ET.fromstring(text)
            # RSS 2.0
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                desc = item.findtext("description", "")
                # strip HTML tags from description
                desc = re.sub(r"<[^>]+>", "", desc or "").strip()[:150] if desc else ""
                if not title or link in seen:
                    continue
                seen.add(link)
                articles.append({
                    "title": title, "description": desc,
                    "url": link, "source": "BBC",
                    "_label": label,
                })
                if len(articles) >= count * 3:
                    break
        except Exception as e:
            logging.warning(f"RSS {label} failed: {e}")
    return articles


def fetch_from_gnews(query: str, count: int) -> list[dict]:
    """Fetch from GNews API (may fail if free tier exhausted)."""
    try:
        resp = requests.get(GNEWS_SEARCH, params={
            "q": query, "lang": "en", "sortby": "publishedAt",
            "max": count * 3, "token": GNEWS_API_KEY,
        }, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if data.get("totalArticles", 0) == 0:
            logging.info(f"  GNews returned 0 articles for '{query}'")
            return []
        seen, articles = set(), []
        for a in data.get("articles", []):
            if not a.get("title") or not a.get("url") or a["url"] in seen:
                continue
            seen.add(a["url"])
            articles.append(a)
        return articles
    except Exception as e:
        logging.warning(f"GNews failed for '{query}': {e}")
        return []


def fetch_all_news() -> list[dict]:
    result = []
    for cat in NEWS_CATEGORIES:
        label, query, count = cat["label"], cat["query"], cat["count"]
        logging.info(f"Fetching {label} ...")
        # Try GNews first
        articles = fetch_from_gnews(query, count)
        if not articles:
            logging.info(f"  Falling back to RSS for {label}")
            articles = fetch_from_rss(label, count)
        logging.info(f"  Got {len(articles)} articles")
        for a in articles:
            a["_label"] = label
        result.extend(articles[: count * 2])
    logging.info(f"Fetched {len(result)} total news articles")
    return result


def fetch_pubmed(count: int) -> list[dict]:
    logging.info("Fetching PubMed papers ...")
    try:
        # Search for recent highly cited/impactful papers
        resp = requests.get(PUBMED_SEARCH, params={
            "db": "pubmed", "term": "last 7 days[edat]",
            "sort": "relevance", "retmax": count * 5,
            "retmode": "json", "api_key": PUBMED_KEY,
        }, timeout=30)
        resp.raise_for_status()
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        logging.info(f"  PubMed found {len(ids)} IDs")
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
            if not title:
                continue
            papers.append({
                "title": title,
                "description": "",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                "source": doc.get("source", "PubMed") or "PubMed",
                "_label": "SCIENTIFIC PAPERS",
            })
        logging.info(f"  Got {len(papers)} PubMed papers")
        return papers[:count]
    except Exception as e:
        logging.warning(f"PubMed fetch failed: {e}")
        return []


def curate_with_llm(items: list[dict]) -> str:
    logging.info(f"Sending {len(items)} items to LLM ({LLM_MODEL}) ...")
    prompt = (
        "You are a news digest curator. Below is a JSON array of news articles "
        "and scientific papers. Curate them into sections.\n"
        "For news: keep 5 economics, 3 science/tech, 2 world.\n"
        "Include all scientific papers as a separate section.\n"
        "Format: section header in ALL CAPS, then for each item:\n"
        "NUMBER. TITLE\n"
        "   One-sentence summary.\n"
        "   URL\n\n"
        "Keep text clean. No markdown code blocks, no JSON output.\n\n"
        + json.dumps(
            [{"title": a["title"], "description": a.get("description",""),
              "url": a["url"], "source": a.get("source","Unknown"),
              "category": a["_label"]}
             for a in items],
            ensure_ascii=False,
        )
    )
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json={"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}], "temperature":0.3},
        headers={
            "Authorization": "Bearer " + OPENROUTER_KEY,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/daily-digest",
        },
        timeout=120,
    )
    if not resp.ok:
        logging.warning(f"LLM returned {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def format_fallback(items: list[dict]) -> str:
    groups = defaultdict(list)
    for a in items:
        groups[a["_label"]].append(a)
    lines = []
    for cat in NEWS_CATEGORIES:
        label, count = cat["label"], cat["count"]
        arts = groups.get(label, [])[:count]
        if arts:
            lines.append(f"\n--- {label} ---")
            for i, a in enumerate(arts, 1):
                lines.append(f"  {i}. {a['title'].strip()}")
                desc = a.get("description", "").strip()
                if desc:
                    lines.append(f"     {desc[:150]}")
                lines.append(f"     {a['url']}")
    papers = groups.get("SCIENTIFIC PAPERS", [])
    if papers:
        lines.append(f"\n--- SCIENTIFIC PAPERS ---")
        for i, p in enumerate(papers, 1):
            lines.append(f"  {i}. {p['title'].strip()}")
            lines.append(f"     {p['url']}")
    return "\n".join(lines)


def send_telegram(text: str):
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
    logging.info(f"Digest sent to Telegram OK")


def main():
    logging.info("=== Daily Digest ===")
    news = fetch_all_news()
    papers = fetch_pubmed(2)
    items = news + papers
    if not items:
        send_telegram("No articles found today.")
        return
    try:
        digest = curate_with_llm(items)
        logging.info("LLM curation succeeded")
    except Exception as e:
        logging.warning(f"LLM failed ({e}), using fallback")
        digest = format_fallback(items)
    send_telegram(digest)
    logging.info("Done!")


if __name__ == "__main__":
    main()
