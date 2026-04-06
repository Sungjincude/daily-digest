#!/usr/bin/env python3
"""Daily article digest bot. 6-7 curated articles + PubMed papers, sent to Telegram."""

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
GN_BASE = "https://news.google.com/rss"

# Track seen URLs for 7 days so we don't repeat
SEEN_FILE = "/tmp/daily_digest_seen.txt"
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


def load_seen_urls():
    try:
        with open(SEEN_FILE) as f:
            urls = set()
            for line in f:
                line = line.strip()
                if line and "|||" in line:
                    date_str, url = line.split("|||", 1)
                    d = datetime.strptime(date_str, "%Y-%m-%d").date()
                    if d >= datetime.now().date() - timedelta(days=SEEN_DAYS):
                        urls.add(url)
            return urls
    except Exception:
        return set()


def save_seen_urls(urls):
    cutoff = datetime.now().date() - timedelta(days=SEEN_DAYS)
    lines = []
    for item in urls:
        if isinstance(item, tuple):
            d, url = item
        else:
            url = item
            d = datetime.now().date()
        if d >= cutoff:
            lines.append(f"{d.strftime('%Y-%m-%d')}|||{url}")
    try:
        with open(SEEN_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def clean_description(text):
    """Remove all HTML tags, URLs, and decode entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_google_news(query, max_items=20):
    url = f"{GN_BASE}/search?q={query}&hl=en-US&gl=US&ceid=US:en"
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
            # Try to get the real article URL
            link_elem = item.find("link")
            raw_link = ""
            if link_elem is not None:
                raw_link = link_elem.text or link_elem.get("href", "")
            # Prefer the source element URL (real article URL)
            source_elem = item.find("{http://purl.org/dc/elements/1.1/}source")
            if source_elem is not None and source_elem.get("url"):
                raw_link = source_elem.get("url")
            if not raw_link:
                raw_link = f"https://www.google.com/search?q={'+'.join(title.split()[:5])}"
            # Clean description
            desc_raw = item.findtext("{http://purl.org/dc/elements/1.1/}description",
                                     item.findtext("description", "") or "")
            desc = clean_description(desc_raw)[:200]
            articles.append({
                "title": title,
                "description": desc,
                "source": title.rsplit(" - ", 1)[-1].strip() if " - " in title else "",
                "url": raw_link,
            })
        return articles
    except Exception as e:
        logging.warning(f"Google News failed: {e}")
        return []


def fetch_gnews(query, count=10):
    try:
        resp = requests.get("https://gnews.io/api/v4/search", params={
            "q": query, "lang": "en", "sortby": "publishedAt",
            "max": count, "token": GNEWS_API_KEY,
        }, timeout=15)
        resp.raise_for_status()
        result = []
        seen = set()
        for a in resp.json().get("articles", []):
            if not a.get("title") or not a.get("url") or a["url"] in seen:
                continue
            seen.add(a["url"])
            desc = re.sub(r"https?://\S+", "", a.get("description", "") or "").strip()[:200]
            result.append({
                "title": a["title"], "description": desc,
                "url": a["url"],
                "source": a.get("source", {}).get("name", "GNews"),
            })
        return result
    except Exception:
        return []


def fetch_articles_for_topic(query, seen_urls, count=8):
    articles = fetch_gnews(query, count)
    gn_articles = fetch_google_news(query)
    articles.extend(gn_articles)
    unique = []
    seen_titles = set()
    for a in articles:
        url = a.get("url", "")
        title_key = a.get("title", "").lower()[:60]
        if url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title_key)
        unique.append(a)
        if len(unique) >= count:
            break
    return unique


def pick_with_llm(article_pools):
    articles_to_send = []
    for label, articles in article_pools.items():
        for a in articles:
            a["_label"] = label
            articles_to_send.append(a)
    if not articles_to_send:
        return {}
    prompt = (
        "You are curating a daily news digest. Below is a list of articles by category.\n"
        "For EACH category, pick EXACTLY ONE article index (0-based) - the most interesting one.\n"
        "Respond with ONLY a JSON object: {\"CategoryName\": index}\n\n"
        + json.dumps(
            [{"title": a["title"], "description": a.get("description",""),
              "source": a.get("source",""), "category": a["_label"]}
             for a in articles_to_send],
            ensure_ascii=False,
        )
    )
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json={"model": LLM_MODEL, "messages": [{"role":"user","content":prompt}], "temperature":0.7},
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
    result = resp.json()["choices"][0]["message"]["content"].strip()
    if result.startswith("```"):
        lines = result.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        result = "\n".join(lines).strip()
    try:
        picked = json.loads(result)
    except json.JSONDecodeError:
        return pick_fallback(article_pools)
    selected = {}
    for label, articles in article_pools.items():
        if label in picked and isinstance(picked[label], int):
            idx = picked[label]
            if 0 <= idx < len(articles_to_send) and articles_to_send[idx].get("_label") == label:
                selected[label] = articles_to_send[idx]
        if label not in selected and articles:
            selected[label] = articles[0]
    return selected


def pick_fallback(article_pools):
    selected = {}
    for label, articles in article_pools.items():
        if articles:
            selected[label] = articles[0]
    return selected


def fetch_pubmed(count=2):
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
            if title:
                papers.append({
                    "title": title, "description": "",
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                    "source": doc.get("source", "PubMed") or "PubMed",
                })
        return papers[:count]
    except Exception as e:
        logging.warning(f"PubMed failed: {e}")
        return []


def generate_bonus_sections():
    """Ask the LLM to generate 5 random fun facts + 5 details about a random place."""
    prompt = (
        "Generate TWO sections:\n\n"
        "Section 1: 5 RANDOM FUN FACTS\n"
        "Five short (1-2 sentences each), surprising, true fun facts about anything "
        "(science, history, animals, language, food, the human body, etc.). "
        "Each must be genuinely interesting and not commonly known.\n\n"
        "Section 2: A RANDOM PLACE\n"
        "Pick one interesting place in the world (a city, region, island, or country — "
        "anything specific). Provide 5 interesting details about it "
        "(history, culture, nature, geography, food, traditions, etc.). "
        "Pick a diverse range over time — don't always pick the same well-known places.\n\n"
        "Format EXACTLY like this, with no extra text:\n\n"
        "--- RANDOM FUN FACTS ---\n"
        "  1. [fact]\n"
        "  2. [fact]\n"
        "  3. [fact]\n"
        "  4. [fact]\n"
        "  5. [fact]\n\n"
        "--- A PLACE: [Place Name] ---\n"
        "  1. [detail]\n"
        "  2. [detail]\n"
        "  3. [detail]\n"
        "  4. [detail]\n"
        "  5. [detail]\n\n"
        "No backticks, no markdown, no extra text before or after."
    )
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.9},
        headers={
            "Authorization": "Bearer " + OPENROUTER_KEY,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/daily-digest",
        },
        timeout=120,
    )
    if not resp.ok:
        logging.warning(f"BONUS LLM {resp.status_code}")
        return None
    result = resp.json()["choices"][0]["message"]["content"].strip()
    # Strip markdown code blocks if any
    if result.startswith("```"):
        lines = result.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        result = "\n".join(lines).strip()
    return result


def format_digest(selected, papers, bonus=None):
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
            url = a.get("url", "").strip()
            if url:
                lines.append(f"  {url}")
            lines.append("")
    if papers:
        lines.append("--- Scientific Papers ---")
        for p in papers:
            lines.append(f"  {p['title'].strip()}")
            lines.append(f"  {p['url']}")
            lines.append("")
    if bonus:
        lines.append(bonus)
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


def main():
    logging.info("=== Daily Digest ===")
    seen_urls = load_seen_urls()
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
    selected = {}
    try:
        selected = pick_with_llm(article_pools)
        logging.info("LLM curation succeeded")
    except Exception as e:
        logging.warning(f"LLM failed ({e}), using fallback")
        selected = pick_fallback(article_pools)
    for label, a in selected.items():
        url = a.get("url", "")
        if url:
            seen_urls.add(url)
    save_seen_urls({(datetime.now().date(), url) for url in seen_urls})
    papers = fetch_pubmed(2)
    for i, p in enumerate(papers, 1):
        logging.info(f"  PubMed {i}: {p['title'][:80]}")
    # Generate bonus fun facts + place section
    bonus = None
    try:
        bonus = generate_bonus_sections()
        if bonus:
            logging.info("Bonus sections generated OK")
    except Exception as e:
        logging.warning(f"Bonus generation failed: {e}")
    digest = format_digest(selected, papers, bonus)
    send_telegram(digest)
    logging.info("Done!")


if __name__ == "__main__":
    main()
