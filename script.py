"""
News watcher for crypto/market-moving headlines.
Checks a set of RSS feeds for keyword combinations and pushes
a notification to your phone via ntfy.sh when something matches.

This does NOT predict the future. It catches news within minutes
of it being published. Always verify before trading on an alert.
"""

import feedparser
import requests
import json
import os

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "changeme-topic")
SEEN_FILE = "seen.json"

# Add/remove RSS feeds here freely - any standard RSS/Atom feed works.
# Feeds do occasionally go stale or change URLs over time - if one breaks,
# the script just skips it and keeps going (see the try/except below), so
# it's safe to leave broken ones in until you notice and swap them out.
FEEDS = [
    # crypto-specific
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/feed",
    # general business / markets
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC top news
    "https://www.cnbc.com/id/10001147/device/rss/rss.html",   # CNBC markets
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://finance.yahoo.com/news/rssindex",
    # tech / AI
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",   # CNBC tech
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    # oil / energy
    "https://oilprice.com/rss/main",
    # world news (for geopolitics that move markets)
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

# Groups with a single keyword list fire when 1+ keyword hits (broad topic watch).
# Groups with two keyword lists ("a" and "b") require one hit from EACH list -
# use that style when a topic is only interesting combined with another (e.g.
# Trump + crypto, rather than either alone flooding you with noise).
KEYWORD_GROUPS = {
    # broad topic watches - any single mention triggers these
    "crypto": ["crypto", "bitcoin", "ethereum", "coin", "token", "stablecoin", "memecoin", "defi", "sec crypto", "solana", "xrp"],
    "ai": ["openai", "chatgpt", "anthropic", "claude ai", "artificial intelligence", " ai model", "nvidia", "gpu chip", "deepseek", "gemini"],
    "big_tech": ["apple", "google", "microsoft", "amazon", "meta platforms", "tesla", "nvidia", "alphabet"],
    "oil_energy": ["opec", "oil price", "crude", "pipeline", "oil supply", "natural gas", "strait of hormuz"],
    "fed_rates": ["federal reserve", "interest rate", "fed chair", "rate cut", "rate hike", "powell", "fomc"],
    "market_moving": ["earnings beat", "earnings miss", "stock plunge", "stock surge", "market sell-off", "recession", "inflation report", "jobs report", "gdp growth", "bankruptcy", "ipo"],
    "geopolitics": ["sanctions", "military strike", "invasion", "ceasefire", "trade war", "tariffs", "war"],

    # narrower combos - need one hit from EACH list to fire, so a common name
    # like "trump" alone (constant in the news) doesn't spam you
    "trump_crypto": {"a": ["trump"], "b": ["coin", "token", "crypto", "bitcoin", "memecoin"]},
    "trump_geopolitics": {"a": ["trump"], "b": ["iran", "strike", "attack", "military", "sanctions", "israel", "china tariff"]},
}


def matches(text, group_def):
    text_l = text.lower()
    if isinstance(group_def, dict):
        # combo group: need 1+ hit from list "a" AND 1+ hit from list "b"
        return any(kw in text_l for kw in group_def["a"]) and any(kw in text_l for kw in group_def["b"])
    # simple group: need just 1 hit from the list
    return any(kw in text_l for kw in group_def)


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()
    return set()


def save_seen(seen):
    # cap growth so the file doesn't balloon forever
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-500:], f)


def send_notification(title, message, url):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Click": url,
                "Priority": "high",
                "Tags": "rotating_light",
            },
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Failed to send notification: {e}")


def main():
    seen = load_seen()
    new_seen = set(seen)

    for feed_url in FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Failed to parse {feed_url}: {e}")
            continue

        for entry in parsed.entries[:20]:
            link = entry.get("link", "")
            if not link or link in seen:
                continue

            title = entry.get("title", "")
            summary = entry.get("summary", "")
            full_text = f"{title} {summary}"

            for group_name, keywords in KEYWORD_GROUPS.items():
                if matches(full_text, keywords):
                    send_notification(
                        title=f"🚨 {group_name.replace('_', ' ').title()}",
                        message=title,
                        url=link,
                    )
                    print(f"Notified [{group_name}]: {title}")
                    break

            new_seen.add(link)

    save_seen(new_seen)


if __name__ == "__main__":
    main()
