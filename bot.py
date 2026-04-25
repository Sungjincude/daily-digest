#!/usr/bin/env python3
"""Daily article digest bot. 7 curated articles + PubMed papers, sent to Telegram."""

import html
import json
import logging
import re
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
LLM_MODEL_ARTICLES = "stepfun/step-3.5-flash:free"
LLM_MODEL_BONUS    = "nousresearch/hermes-3-llama-3.1-405b:free"
GN_BASE = "https://news.google.com/rss"

# Persistent seen-articles file stored in the repo (persists between GH Actions runs)
SEEN_FILE = "seen_articles.json"
SEEN_DAYS = 14  # track for 2 weeks so repeats are even less likely

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


def load_seen_articles():
    """Load seen articles from persistent JSON file in the repo."""
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
            cutoff = datetime.now().date() - timedelta(days=SEEN_DAYS)
            urls = set()
            titles = set()
            for entry in data:
                d = datetime.strptime(entry["date"], "%Y-%m-%d").date()
                if d >= cutoff:
                    if entry.get("url"):
                        urls.add(entry["url"])
                    if entry.get("title"):
                        titles.add(entry["title"][:80].lower())
            return {"urls": urls, "titles": titles}
    except Exception:
        return {"urls": set(), "titles": set()}


def save_seen_articles(url, title):
    """Append a seen article to the persistent JSON file."""
    data = []
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
    except Exception:
        pass
    cutoff = datetime.now().date() - timedelta(days=SEEN_DAYS)
    data = [e for e in data if datetime.strptime(e["date"], "%Y-%m-%d").date() >= cutoff]
    data.append({
        "date": datetime.now().date().strftime("%Y-%m-%d"),
        "url": url or "",
        "title": (title or "")[:80].lower(),
    })
    with open(SEEN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def title_similarity(t1, t2):
    """Check if two titles are similar enough to be the same article (~60% shared words)."""
    words1 = set(t1.lower().split())
    words2 = set(t2.lower().split())
    if not words1 or not words2:
        return False
    overlap = len(words1 & words2)
    return overlap / min(len(words1), len(words2)) >= 0.6


def clean_description(text):
    """Aggressively remove ALL HTML, entities, and URLs from text."""
    if not text:
        return ""
    text = html.unescape(text)
    for _ in range(3):
        text = re.sub(r"<[^>]*>", "", text)
        text = re.sub(r"<[^>]+$", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"www\.\S+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_google_news_url(desc_raw, link_elem, title):
    """Extract the actual Google News redirect URL from RSS.

    The <link> element in Google News RSS is sometimes the same
    as the href in the description — a news.google.com/articles/...
    URL that redirects through browser into the real article.
    """
    href_match = re.search(r'href="(https://news\.google\.com/rss/articles/[^"]+)"', desc_raw)
    if href_match:
        return href_match.group(1)
    if link_elem:
        return link_elem
    return f"https://www.google.com/search?q={'+'.join(title.split()[:5])}"


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
            link_elem = item.findtext("link", "").strip()
            desc_raw = item.findtext("{http://purl.org/dc/elements/1.1/}description",
                                     item.findtext("description", "") or "")
            article_url = extract_google_news_url(desc_raw, link_elem, title)
            desc = clean_description(desc_raw)[:200]
            articles.append({
                "title": title,
                "description": desc,
                "source": title.rsplit(" - ", 1)[-1].strip() if " - " in title else "",
                "url": article_url,
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


def fetch_bbc_rss(topic_query, count=10):
    """Fetch from BBC News RSS as an extra source for variety."""
    feeds = [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "https://feeds.bbci.co.uk/news/health/rss.xml",
    ]
    query_words = set(topic_query.lower().split())
    articles = []
    for feed_url in feeds:
        try:
            resp = requests.get(feed_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            })
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                desc_raw = item.findtext("description", "") or ""
                if not title:
                    continue
                # Filter: at least one query word should match
                title_words = set(title.lower().split())
                if not (query_words & title_words):
                    continue
                desc = clean_description(desc_raw)[:200]
                articles.append({
                    "title": title, "description": desc,
                    "url": link,
                    "source": "BBC News",
                })
                if len(articles) >= count:
                    return articles
        except Exception:
            pass
    return articles


def fetch_articles_for_topic(query, seen_data, count=8):
    """Fetch from multiple sources, filter out seen articles by URL AND title similarity."""
    combined = []
    combined.extend(fetch_gnews(query, count))
    combined.extend(fetch_google_news(query))
    combined.extend(fetch_bbc_rss(query, count))

    unique = []
    seen_urls = seen_data["urls"]
    seen_titles = seen_data["titles"]
    for a in combined:
        url = a.get("url", "")
        title = a.get("title", "")
        if url in seen_urls:
            continue
        # Also skip if title is very similar to a previously sent article
        if any(title_similarity(title, st) for st in seen_titles):
            continue
        seen_urls.add(url)
        title_key = title.lower()[:60]
        if any(title_similarity(title_key, t) for t in [t for t in seen_titles if len(t) >= 60]):
            continue
        unique.append(a)
        if len(unique) >= count:
            break
    return unique


def call_llm(model, messages, temperature=0.7, timeout=120):
    """Generic LLM call helper."""
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json={"model": model, "messages": messages, "temperature": temperature},
        headers={
            "Authorization": "Bearer " + OPENROUTER_KEY,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/daily-digest",
        },
        timeout=timeout,
    )
    if not resp.ok:
        logging.warning(f"LLM ({model}) {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    result = resp.json()["choices"][0]["message"]["content"].strip()
    if result.startswith("```"):
        lines = result.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        result = "\n".join(lines).strip()
    return result


def pick_with_llm(article_pools):
    """Use the articles LLM to pick one article per topic."""
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
    try:
        result = call_llm(LLM_MODEL_ARTICLES, [{"role":"user","content":prompt}], temperature=0.7)
        picked = json.loads(result)
    except (json.JSONDecodeError, Exception) as e:
        logging.warning(f"LLM curation failed ({e}), using fallback")
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
    """Use the bonus LLM to generate 5 random fun facts + 5 details about a random place."""
    prompt = (
        "Generate TWO sections. ALL TEXT MUST BE IN GREEK (except place names and "
        "technical terms like proper nouns can stay in English).\n\n"
        "Section 1: 5 ΤΥΧΑΙΑ ΕΝΔΙΑΦΕΡΟΝΤΑ ΓΕΓΟΝΟΤΑ\n"
        "Five short (1-2 sentences each), surprising, true fun facts about anything "
        "(science, history, animals, language, food, the human body, etc.). "
        "Each must be genuinely interesting and not commonly known.\n\n"
        "Section 2: ΕΝΑ ΜΕΡΟΣ ΣΤΟΝ ΚΟΣΜΟ\n"
        "Pick one interesting place in the world (a city, region, island, or country — "
        "anything specific). Provide 5 interesting details about it "
        "(history, culture, nature, geography, food, traditions, etc.). "
        "Each time pick a different place - rotate between continents, "
        "avoid the most famous tourist destinations.\n\n"
        "Format EXACTLY like this, with no extra text:\n\n"
        "--- ΤΥΧΑΙΑ ΕΝΔΙΑΦΕΡΟΝΤΑ ΓΕΓΟΝΟΤΑ ---\n"
        "  1. [fact]\n"
        "  2. [fact]\n"
        "  3. [fact]\n"
        "  4. [fact]\n"
        "  5. [fact]\n\n"
        "--- ΜΕΡΟΣ: [Place Name] ---\n"
        "  1. [detail]\n"
        "  2. [detail]\n"
        "  3. [detail]\n"
        "  4. [detail]\n"
        "  5. [detail]\n\n"
        "No backticks, no markdown, no JSON, no extra text before or after. "
        "Write naturally in Greek."
    )
    try:
        result = call_llm(LLM_MODEL_BONUS, [{"role":"user","content":prompt}], temperature=0.9)
        # Strip markdown code blocks if any
        if result.startswith("```"):
            lines = result.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            result = "\n".join(lines).strip()
        if result:
            logging.info("Bonus sections generated OK")
            return result
    except Exception as e:
        logging.warning(f"BONUS LLM failed: {e}")
    logging.warning("Bonus generation returned empty, using fallback")
    return fallback_bonus_message()


def fallback_bonus_message():
    """Hardcoded fallback bonus sections in Greek, used when the LLM fails."""
    return (
        "--- ΤΥΧΑΙΑ ΕΝΔΙΑΦΕΡΟΝΤΑ ΓΕΓΟΝΟΤΑ ---\n"
        "  1. Ο εγκέφαλος του ανθρώπου μπορεί να αποθηκεύσει περίπου 2,5 petabytes δεδομένων — ισοδύναμα με 300 χρόνια συνεχούς τηλεόρασης.\n"
        "  2. Το μέλι δεν χαλάει ποτέ. Αρχαιολόγοι έχουν βρει μέλι 3.000 ετών σε αιγυπτιακούς τάφους που ήταν ακόμη βρώσιμο.\n"
        "  3. Υπάρχουν περισσότερα άστρα στο σύμπαν από κόκκους άμμου σε όλες τις παραλίες της Γης.\n"
        "  4. Οι χταπόδια έχουν τρεις καρδιές και μπλε αίμα.\n"
        "  5. Η γλώσσα των δελφινιών περιλαμβάνει «ονόματα» — χρησιμοποιούν συγκεκριμένους ήχους για να αναφέρονται ο ένας στον άλλον.\n\n"
        "--- ΜΕΡΟΣ: Νησιά Φερόε (Βόρειος Ατλαντικός) ---\n"
        "  1. Είναι ένα αυτόνομο νησιωτικό σύμπλεγμα ανάμεσα στη Νορβηγία και την Ισλανδία, με πληθυσμό μόλις ~53.000.\n"
        "  2. Υπάρχουν σχεδόν διπλάσια πρόβατα από ανθρώπους — από εδώ προέρχεται και το όνομα «Φερόε» (νησιά των προβάτων).\n"
        "  3. Τα γραφικά τοπία τους περιλαμβάνουν τους ψηλότερους γκρεμούς στην Ευρώπη, με ύψος πάνω από 800 μέτρα.\n"
        "  4. Έχουν τη δική τους γλώσσα, τα Φαρόε, που προέρχεται από την αρχαία Νορδική.\n"
        "  5. Κάθε καλοκαίρι γίνεται το παραδοσιακό «Grindadráp» — κυνήγι φάλαινων που αποτελεί μέρος της πολιτιστικής κληρονομιάς."
    )


def format_articles(selected, papers):
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
    return "\n".join(lines)


def send_telegram_two_messages(articles_text, bonus_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = f"Good morning! Daily digest - {today}"

    # Message 1: Articles
    msg1 = header + "\n\n" + articles_text
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT, "text": msg1[:4000],
        "disable_web_page_preview": True,
    }, timeout=30)
    resp.raise_for_status()
    logging.info("Articles sent to Telegram")

    # Message 2: Bonus
    if bonus_text:
        msg2 = f" Extra bits - {today}\n\n" + bonus_text
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT, "text": msg2[:4000],
            "disable_web_page_preview": True,
        }, timeout=30)
        resp.raise_for_status()
        logging.info("Bonus sent to Telegram")


def main():
    logging.info("=== Daily Digest ===")
    seen_data = load_seen_articles()
    logging.info(f"Loaded {len(seen_data['urls'])} seen URLs, {len(seen_data['titles'])} seen titles from memory")

    article_pools = {}
    news = []
    for topic in TOPICS:
        label, query = topic["label"], topic["query"]
        logging.info(f"Fetching {label} ...")
        pool = fetch_articles_for_topic(query, seen_data, 8)
        article_pools[label] = pool
        for a in pool:
            a["_label"] = label
            news.append(a)
        logging.info(f"  Got {len(pool)} unique articles")

    selected = pick_with_llm(article_pools)

    # Save selected articles to persistent memory
    for label, a in selected.items():
        url = a.get("url", "")
        title = a.get("title", "")
        save_seen_articles(url, title)

    papers = fetch_pubmed(2)
    for i, p in enumerate(papers, 1):
        logging.info(f"  PubMed {i}: {p['title'][:80]}")

    # Generate bonus (uses separate LLM)
    bonus = generate_bonus_sections()

    articles_text = format_articles(selected, papers)
    send_telegram_two_messages(articles_text, bonus)
    logging.info("Done!")


if __name__ == "__main__":
    main()
