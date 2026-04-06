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

TOPICS = [
    {
        "label": "Finance",
        "keywords": ["finance", "banking", "currency", "debt", "stock", "wall street", "invest", "bank", "interest rate", "inflation"],
        "feeds": [
            "https://feeds.bbci.co.uk/news/business/rss.xml",
            "https://finance.yahoo.com/news/rssindex",
        ],
    },
    {
        "label": "Business",
        "keywords": ["business", "company", "industry", "trade", "market", "corporate", "startup", "merger", "acquisition"],
        "feeds": [
            "https://feeds.bbci.co.uk/news/business/rss.xml",
            "https://www.reuters.com/rssFeed/business",
        ],
    },
    {
        "label": "Technology",
        "keywords": ["technology", "software", "AI", "artificial intelligence", "cyber", "data", "computing", "tech", "digital"],
        "feeds": [
            "https://feeds.bbci.co.uk/news/technology/rss.xml",
            "https://www.wired.com/feed/rss",
        ],
    },
    {
        "label": "Animals",
        "keywords": ["animal", "wildlife", "endangered", "conservation", "species", "pet", "dog", "cat", "ocean", "marine", "bird"],
        "feeds": [
            "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
            "https://news.google.com/rss/search?q=animals+wildlife&hl=en-US&gl=US&ceid=US:en",
        ],
    },
    {
        "label": "Science",
        "keywords": ["science", "research", "discovery", "experiment", "medicine", "biology", "physics", "space", "NASA", "DNA"],
        "feeds": [
            "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
            "https://news.google.com/rss/search?q=science+research+discovery&hl=en-US&gl=US&ceid=US:en",
        ],
    },
    {
        "label": "Arts and Travel",
        "keywords": ["arts", "culture", "travel", "museum", "music", "film", "painting", "tourism", "festival", "exhibition"],
        "feeds": [
            "https://www.theguardian.com/world/rss",
            "https://www.theguardian.com/travel/rss",
            "https://news.google.com/rss/search?q=arts+culture+travel&hl=en-US&gl=US&ceid=US:en",
        ],
    },
]

PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def clean_html(s):
    return re.sub(r"<[^>]+>", "", s).strip()


def matches_keywords(text, keywords):
    """Check if text contains any of the topic keywords (case insensitive)."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def fetch_rss(feed_url):
    """Fetch all articles from a single RSS feed."""
    try:
        r = requests.get(feed_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if not r.ok:
            return []
        # Some feeds have encoding issues
        text = r.text.replace("]]>", "").strip()
        if not text.startswith("<?xml") and not text.startswith("<rss"):
            # Could be RSS with BOM or encoding declaration
            pass

        root = ET.fromstring(text.encode("utf-8"))
        articles = []
        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = clean_html(item.findtext("description", "") or "")
            if not title or not link:
                continue
            articles.append({"title": title, "url": link, "description": desc[:200], "source": "RSS"})
        return articles
    except Exception as e:
        logging.warning(f"  RSS feed failed ({feed_url}): {e}")
        return []


def fetch_one_article_per_topic() -> list[dict]:
    """For each topic, try all feeds, pick the first article matching keywords."""
    result = []
    for topic in TOPICS:
        label = topic["label"]
        keywords = topic["keywords"]
        logging.info(f"Fetching {label} ...")

        found = None
        for feed_url in topic["feeds"]:
            if found:
                break
            logging.info(f"  Trying feed: {feed_url}")
            articles = fetch_rss(feed_url)
            logging.info(f"    Feed returned {len(articles)} articles")
            for a in articles:
                combined = (a["title"] + " " + a["description"]).lower()
                if matches_keywords(a["title"] + " " + a["description"], keywords):
                    found = a
                    found["_label"] = label
                    logging.info(f"  MATCH: {found['title'][:80]}")
                    break

        if found:
            result.append(found)
        else:
            logging.warning(f"  NO MATCH for {label}")

    logging.info(f"Fetched {len(result)}/{len(TOPICS)} articles")
    return result


def fetch_pubmed(count: int) -> list[dict]:
    logging.info("Fetching PubMed papers ...")
    try:
        resp = requests.get(PUBMED_SEARCH, params={
            "db": "pubmed", "term": "last 7 days[edat]",
            "sort": "relevance", "retmax": count * 5,
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
    prompt = (
        "You are writing a daily news digest. Below are recent articles and scientific papers.\n\n"
        "Group them by their category (shown as _label).\n"
        "Format each item as:\n"
        "TITLE\n"
        "   One-sentence summary\n"
        "   URL\n\n"
        "Keep it clean. No markdown code blocks, no backticks.\n\n"
        + json.dumps(
            [{"title": a["title"], "description": a.get("description",""),
              "url": a["url"], "source": a.get("source",""),
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
    lines = []
    for topic in TOPICS:
        label = topic["label"]
        arts = [a for a in items if a.get("_label") == label]
        if arts:
            a = arts[0]
            lines.append(f"--- {label} ---")
            lines.append(f"  {a['title'].strip()}")
            desc = a.get("description", "").strip()
            if desc:
                lines.append(f"  {desc[:150]}")
            lines.append(f"  {a['url']}")
            lines.append("")
    papers = [a for a in items if a.get("_label") == "SCIENTIFIC PAPERS"]
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
    news = fetch_one_article_per_topic()
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
