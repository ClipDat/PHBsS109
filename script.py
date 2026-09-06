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

# Add/remove RSS feeds here freely - any standard RSS/Atom feed works
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://oilprice.com/rss/main",
]

# Each group needs at least 2 of its keywords present in the headline+summary
# to fire. Tune these freely - this is the main "brain" of the whole thing.
KEYWORD_GROUPS = {
    "trump_crypto": ["trump", "coin", "token", "crypto", "bitcoin", "memecoin", "ethereum"],
    "trump_iran": ["trump", "iran", "strike", "attack", "military", "sanctions", "israel"],
    "fed_rates": ["federal reserve", "interest rate", "fed chair", "rate cut", "rate hike", "powell"],
    "oil_supply": ["opec", "oil price", "crude", "pipeline", "strait of hormuz", "oil supply"],
}


def matches(text, group_keywords, min_hits=2):
    text_l = text.lower()
    hits = sum(1 for kw in group_keywords if kw in text_l)
    return hits >= min_hits


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
