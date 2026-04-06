#!/usr/bin/env python3
"""Daily article digest bot. Fetches fresh articles (no repeats), curates with LLM, sends to Telegram."""

import json
import logging
import os
import xml.etree.ElementTree as ET
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
GN_BASE = "https://news.google.com/rss"

# Keep seen URLs for 7 days to avoid repeats
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_urls.txt")
SEEN_DAYS = 7

TOPICS = [
    {"label": "Finance",         "query": "finance banking currency economy inflation interest rates"},
    {"label": "Business",        "query": "business companies market industry trade corporate"},
    {"label": "Technology",      "query": "technology software AI artificial intelligence computing"},
    {"label": "Animals",         "query": "animals wildlife species conservation endangered"},
    {"label": "Science",         "query": "science research discovery medicine biology physics"},
    {"label": "History",         "query": "history historical ancient archaeology civilization"},
    {"label": "Arts and Travel", "query": "arts culture travel tourism heritage museum festival"},
]

PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


# ── Seen URL tracking (avoids repeats for SEEN_DAYS) ────────────────────────
def load_seen():
    """Load set of URLs seen recently."""
    seen = set()
    if not os.path.exists(SEEN_FILE):
        return seen
    try:
        with open(SEEN_FILE) as f:
            for line in f:
                line = line.strip()
                if line and line != "":
                    parts = line.split("|||", 1)
                    if len(parts) == 2:
                        date_str, url = parts
                        d = datetime.strptime(date_str, "%Y-%m-%d").date()
                        if d >= datetime.now().date() - timedelta(days=SEEN_DAYS):
                            seen.add(url)
    except Exception:
        pass
    return seen


def save_seen(seen):
    """Save seen URLs with dates (auto-cleans after SEEN_DAYS)."""
    cutoff = datetime.now().date() - timedelta(days=SEEN_DAYS)
    lines = []
    for url, d in seen.items():
        if d >= cutoff:
            lines.append(f"{d.strftime('%Y-%m-%d')}|||{url}")
    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


# ── Fetch from multiple sources ─────────────────────────────────────────────
def fetch_google_news(query, max_items=20):
    """Fetch from Google News RSS."""
    url = f"{GN_BASE}/search?q={query}&hl=en-US&gl=US&ceid=US:en&num={max_items}"
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })
        resp.raise_for_status()
        text = resp.text.replace("]]>", "")
        root = ET.fromstring(text)
        articles = []
        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            if not title:
                continue
            articles.append({
                "title": title,
                "description": item.findtext("description", "").strip()[:200],
                "source": "Google News",
                "url": f"https://news.google.com/search?q={'+'.join(title.split()[:4])}",
            })
        return articles
    except Exception as e:
        logging.warning(f"Google News failed: {e}")
        return []


def fetch_gnews(query, count=10):
    """Fetch from GNews API (if quota available)."""
    try:
        resp = requests.get("https://gnews.io/api/v4/search", params={
            "q": query, "lang": "en", "sortby": "publishedAt",
            "max": count, "token": GNEWS_API_KEY,
        }, timeout=15)
        resp.raise_for_status()
        result = []
        for a in resp.json().get("articles", []):
            if a.get("title") and a.get("url"):
                result.append({
                    "title": a["title"], "description": a.get("description", "")[:200],
                    "source": a.get("source", {}).get("name", "GNews"),
                    "url": a["url"],
                })
        return result
    except Exception:
        return []


def fetch_articles_for_topic(query, seen_urls, count=8):
    """Fetch articles from GNews first (more variety), fallback to Google News."""
    # Try GNews first — it returns different results due to different algorithm
    articles = fetch_gnews(query, count)
    # Also get Google News for backup
    gn_articles = fetch_google_news(query)
    # Combine: GNews articles first (more variety), then Google News
    articles.extend(gn_articles)

    # Deduplicate by title similarity and filter out already-seen URLs
    unique = []
    seen_titles = set()
    for a in articles:
        url = a.get("url", "")
        title_key = a["title"].lower()[:60]  # normalize for similarity
        if url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title_key)
        unique.append(a)
        if len(unique) >= count:
            break

    return unique


# ── LLM picks one best article per topic ────────────────────────────────────
def pick_with_llm(article_pools):
    """Send all candidate articles to LLM, let it pick 1 per topic."""
    articles_to_send = []
    for label, articles in article_pools.items():
        for a in articles:
            a["_label"] = label
            articles_to_send.append(a)

    if not articles_to_send:
        return {}

    prompt = (
        "You are curating a daily news digest. Below is a list of articles by category.\n"
        "For EACH category, pick EXACTLY ONE article — the most interesting or important one.\n"
        "Try to pick different articles each day, not the most obvious headline.\n"
        "Respond with a JSON object mapping each category label to the article's index (0-based).\n"
        "Only output valid JSON, nothing else.\n\n"
        + json.dumps(
            [{"title": a["title"], "description": a.get("description", ""),
              "source": a.get("source", ""), "category": a["_label"]}
             for a in articles_to_send],
            ensure_ascii=False,
        )
    )

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7},
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

    result = resp.json()["choices"][0]["message"]["content"]
    # Parse JSON from response (may have markdown code blocks)
    result = result.strip()
    if result.startswith("```"):
        result = result[result.index("\n")+1:]
        if result.endswith("```"):
            result = result[:-3]
    result = result.strip()
    picked = json.loads(result)

    # Build selected articles
    selected = {}
    idx = 0
    for label, articles in article_pools.items():
        if label in picked:
            chosen_idx = picked[label]
            if isinstance(chosen_idx, int) and 0 <= chosen_idx < len(articles_to_send):
                article = articles_to_send[chosen_idx]
                if article["_label"] == label:
                    selected[label] = article
        # Fallback: first article from pool
        if label not in selected and articles:
            selected[label] = articles[0]

    return selected


def pick_fallback(article_pools):
    """Non-LLM fallback: pick newest article per topic."""
    selected = {}
    for label, articles in article_pools.items():
        if articles:
            selected[label] = articles[0]  # Most recent
    return selected


# ── PubMed ──────────────────────────────────────────────────────────────────
def fetch_pubmed(count=2):
    logging.info("Fetching PubMed papers ...")
    try:
        resp = requests.get(PUBMED_SEARCH, params={
            "db": "pubmed", "term": "last 7 days[edat]",
            "sort": "relevance", "retmax": count * 5,
            "retmode": "json", "api_key": PUBMED_KEY,
        }, timeout=30)
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        resp = requests.get(PUBMED_SUMMARY, params={
            "db": "pubmed", "id": ",".join(ids[:count*3]),
            "retmode": "json", "api_key": PUBMED_KEY,
        }, timeout=30)
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
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                "source": doc.get("source", "PubMed") or "PubMed",
            })
        return papers[:count]
    except Exception as e:
        logging.warning(f"PubMed failed: {e}")
        return []


# ── Format & send ───────────────────────────────────────────────────────────
def format_digest(selected, papers):
    lines = []
    for topic in TOPICS:
        label = topic["label"]
        if label in selected:
            a = selected[label]
            lines.append(f"--- {label} ---")
            lines.append(f"  {a['title'].strip()}")
            if a.get("source"):
                lines.append(f"  ({a['source']})")
            if a.get("description"):
                lines.append(f"  {a['description'].strip()[:150]}")
            lines.append(f"  {a.get('url', '')}")
            lines.append("")
    if papers:
        lines.append("--- Scientific Papers ---")
        for p in papers:
            lines.append(f"  {p['title'].strip()}")
            lines.append(f"  {p['url']}")
            lines.append("")
    return "\n".join(lines)


def send_telegram(text):
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


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    logging.info("=== Daily Digest ===")
    seen_urls = load_seen()

    # Fetch articles for each topic
    article_pools = {}
    news = []
    for topic in TOPICS:
        label, query = topic["label"], topic["query"]
        logging.info(f"Fetching {label} ...")
        pool = fetch_articles_for_topic(query, set(seen_urls) | {a.get("url") for a in news}, 8)
        article_pools[label] = pool
        for a in pool:
            a["_label"] = label
            news.append(a)
        logging.info(f"  Got {len(pool)} unique articles")

    # LLM picks 1 per topic
    selected = {}
    try:
        selected = pick_with_llm(article_pools)
        logging.info("LLM curation succeeded")
    except Exception as e:
        logging.warning(f"LLM failed ({e}), using fallback")
        selected = pick_fallback(article_pools)

    # Track seen URLs
    for label, a in selected.items():
        url = a.get("url", "")
        if url:
            seen_urls.add(url)
    save_seen({u: datetime.now().date() for u in seen_urls})

    # PubMed papers
    papers = fetch_pubmed(2)
    for i, p in enumerate(papers, 1):
        logging.info(f"  PubMed {i}: {p['title'][:80]}")

    # Format and send
    digest = format_digest(selected, papers)
    send_telegram(digest)
    logging.info("Done!")


if __name__ == "__main__":
    main()
