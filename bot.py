#!/usr/bin/env python3
"""Daily article digest bot. 6-7 curated articles + PubMed papers, sent to Telegram.

Uses Google News RSS (free, no API key, always works) + PubMed + GNews fallback.
"""

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

# Google News RSS feed - no API key, always free
GN_BASE = "https://news.google.com/rss"

TOPICS = [
    {
        "label": "Finance",
        "query": "finance banking currency economy inflation interest rates",
    },
    {
        "label": "Business",
        "query": "business companies market industry trade corporate",
    },
    {
        "label": "Technology",
        "query": "technology software AI artificial intelligence computing",
    },
    {
        "label": "Animals",
        "query": "animals wildlife species conservation endangered",
    },
    {
        "label": "Science",
        "query": "science research discovery medicine biology physics",
    },
    {
        "label": "History",
        "query": "history historical ancient archaeology civilization historical discovery",
    },
    {
        "label": "Arts and Travel",
        "query": "arts culture travel tourism heritage museum festival",
    },
]

PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def fetch_google_news(query: str) -> list[dict]:
    """Fetch from Google News RSS with a specific search query."""
    url = f"{GN_BASE}/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        resp.raise_for_status()
        text = resp.text.replace("]]>", "").strip()
        root = ET.fromstring(text)
        articles = []
        for item in root.iter("item"):
            title_elem = item.findtext("title", "").strip()
            # Google News titles are "Title - Source"
            # Split on last " - " to get source
            parts = title_elem.rsplit(" - ", 1)
            title = parts[0].strip() if len(parts) > 1 else title_elem.strip()
            source = parts[-1].strip() if len(parts) > 1 else "Unknown"
            link_elem = item.find("link")
            link = link_elem.get("href", "") if link_elem is not None else ""
            # Google News links are relative, need to fix
            if link.startswith("/articles/"):
                link = f"https://news.google.com{link}"
            desc = item.findtext("description", "").strip()
            pub_date = item.findtext("pubDate", "").strip()
            if not title:
                continue
            articles.append({
                "title": title,
                "url": f"https://news.google.com/search?q={title.replace(' ', '+')}",
                "description": desc[:200] if desc else "",
                "source": source,
                "published": pub_date,
            })
        return articles
    except Exception as e:
        logging.warning(f"Google News failed for '{query}': {e}")
        return []


def fetch_gnews_fallback(query: str, count: int) -> list[dict]:
    """Try GNews if Google News fails."""
    try:
        resp = requests.get("https://gnews.io/api/v4/search", params={
            "q": query, "lang": "en", "sortby": "publishedAt",
            "max": count * 3, "token": GNEWS_API_KEY,
        }, timeout=20)
        resp.raise_for_status()
        result = []
        seen = set()
        for a in resp.json().get("articles", []):
            if not a.get("title") or not a.get("url") or a["url"] in seen:
                continue
            seen.add(a["url"])
            result.append(a)
        return result
    except Exception:
        return []


def fetch_one_article_per_topic() -> list[dict]:
    """Fetch 1 article per topic from Google News, fallback to GNews."""
    result = []
    for topic in TOPICS:
        label = topic["label"]
        query = topic["query"]
        logging.info(f"Fetching {label}: '{query}' ...")

        articles = fetch_google_news(query)
        if not articles:
            logging.info(f"  Google News returned 0, trying GNews ...")
            articles = fetch_gnews_fallback(query, 3)

        if articles:
            article = articles[0]  # Most recent
            article["_label"] = label
            result.append(article)
            logging.info(f"  Got: {article['title'][:80]}")
        else:
            logging.warning(f"  NO articles found for {label}")

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
        "Keep it clean. No markdown code blocks, no backticks, no asterisks.\n\n"
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
    # Check if it's roughly 8 AM Greece time (Athens tz)
    athens_tz = timezone(timedelta(hours=2)) if False else None
    # Use a proper timezone-aware check: 5 or 6 AM UTC covers 7-8 AM EET / 8 AM EEST
    try:
        import zoneinfo
        athens = datetime.now(zoneinfo.ZoneInfo("Europe/Athens"))
    except (ImportError, zoneinfo.ZoneInfoNotFoundError):
        athens = datetime.now(timezone.utc) + timedelta(hours=2)
    greece_hour = athens.hour
    logging.info(f"It is {greece_hour}:00 in Greece")
    if greece_hour < 6 or greece_hour > 10:
        logging.info("Not 8 AM Greece time, skipping (manual trigger detected)")
        # Still run if manually triggered, just warn
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
