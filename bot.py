#!/usr/bin/env python3
"""Daily article digest bot. 6 curated articles + PubMed papers, sent to Telegram."""

import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone

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

# Exactly 6 topics, 1 article each
TOPICS = [
    {"label": "Finance",         "query": "finance banking currency exchange rates debt",       "feed": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    {"label": "Business",        "query": "business companies market industry trade economy",    "feed": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    {"label": "Technology",      "query": "technology software hardware AI startups",           "feed": "https://feeds.bbci.co.uk/news/technology/rss.xml"},
    {"label": "Animals",         "query": "animals wildlife pets endangered species nature",    "feed": "https://www.google.com/rss"},
    {"label": "Science",         "query": "science research discovery experiment medicine",     "feed": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    {"label": "Arts and Travel", "query": "arts culture travel museum music painting",          "feed": "https://feeds.bbci.co.uk/news/arts/rss.xml"},
]

GNEWS_SEARCH  = "https://gnews.io/api/v4/search"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def fetch_rss(label: str, feed_url: str) -> list[dict]:
    """Fetch articles from an RSS feed."""
    try:
        r = requests.get(feed_url, timeout=20)
        if not r.ok:
            return []
        text = r.text.replace("]]>", "")
        root = ET.fromstring(text)
        articles = []
        seen = set()
        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = re.sub(r"<[^>]+>", "", item.findtext("description", "") or "").strip()[:150]
            if not title or link in seen:
                continue
            # Filter: reject if it doesn't match the topic keywords
            articles.append({
                "title": title, "description": desc, "url": link,
                "source": "BBC", "_label": label,
            })
        return articles
    except Exception as e:
        logging.warning(f"RSS {label} failed: {e}")
        return []


def fetch_gnews(query: str, label: str) -> list[dict]:
    """Fetch articles from GNews."""
    try:
        resp = requests.get(GNEWS_SEARCH, params={
            "q": query, "lang": "en", "sortby": "publishedAt",
            "max": 10, "token": GNEWS_API_KEY,
        }, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        articles = []
        seen = set()
        for a in data.get("articles", []):
            if not a.get("title") or not a.get("url") or a["url"] in seen:
                continue
            seen.add(a["url"])
            articles.append(a)
            articles[-1]["_label"] = label
        return articles
    except Exception as e:
        logging.warning(f"GNews failed for '{query}': {e}")
        return []


def fetch_all_news() -> list[dict]:
    """Fetch 1 article per topic from GNews, fallback to RSS."""
    result = []
    for topic in TOPICS:
        logging.info(f"Fetching {topic['label']} ...")

        # Try GNews first
        articles = fetch_gnews(topic["query"], topic["label"])
        if not articles:
            logging.info(f"  Falling back to RSS for {topic['label']}")
            articles = fetch_rss(topic["label"], topic["feed"])

        if articles:
            result.append(articles[0])  # Take the best (most recent) one
            logging.info(f"  Got: {articles[0]['title'][:80]}")
        else:
            logging.warning(f"  No articles for {topic['label']}")

    logging.info(f"Fetched {len(result)} articles (goal: {len(TOPICS)})")
    return result


def fetch_pubmed(count: int) -> list[dict]:
    """Fetch recent scientific papers from PubMed."""
    logging.info("Fetching PubMed papers ...")
    try:
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
                "title": title, "description": "",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                "source": doc.get("source", "PubMed") or "PubMed",
                "_label": "SCIENTIFIC PAPERS",
            })
        return papers[:count]
    except Exception as e:
        logging.warning(f"PubMed fetch failed: {e}")
        return []


def curate_with_llm(items: list[dict]) -> str:
    """LLM curates and writes summaries."""
    prompt = (
        "You are writing a daily news digest. Below are recent articles and papers.\n\n"
        "For the news articles, group them by their category (label).\n"
        "For scientific papers, put them under SCIENTIFIC PAPERS.\n\n"
        "Format each article as:\n"
        "TITLE\n"
        "   One-sentence summary\n"
        "   URL\n\n"
        "Keep it clean and readable. No markdown code blocks.\n\n"
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
        logging.warning(f"LLM {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def format_fallback(items: list[dict]) -> str:
    """Simple formatting without LLM."""
    groups = defaultdict(list)
    for a in items:
        groups[a["_label"]].append(a)
    lines = []
    for topic in TOPICS:
        label = topic["label"]
        arts = groups.get(label, [])
        if arts:
            a = arts[0]
            lines.append(f"--- {label} ---")
            lines.append(f"  {a['title'].strip()}")
            desc = a.get("description", "").strip()
            if desc:
                lines.append(f"  {desc[:150]}")
            lines.append(f"  {a['url']}")
            lines.append("")
    papers = groups.get("SCIENTIFIC PAPERS", [])
    if papers:
        lines.append("--- SCIENTIFIC PAPERS ---")
        for p in papers:
            lines.append(f"  {p['title'].strip()}")
            lines.append(f"  {p['url']}")
            lines.append("")
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
    logging.info("Digest sent to Telegram OK")


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
