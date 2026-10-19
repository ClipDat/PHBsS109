"""
News watcher for crypto/market-moving headlines.
Sends ONE digest notification per run with all matched headlines (with links)
and which stocks may go up / down / be affected. Uses GitHub Models (free)
for the analysis, with a simple ticker-map fallback if the AI call fails.

This does NOT predict the future and is not financial advice.
"""

import feedparser
import requests
import json
import os

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "changeme-topic")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
SEEN_FILE = "seen.json"

AI_MODEL = "openai/gpt-4o-mini"   # any model from the GitHub Models catalog
MAX_HEADLINES = 10                # per digest; extras roll into the next run
MAX_MESSAGE_CHARS = 3600          # ntfy messages are limited to ~4KB

FEEDS = [
    # crypto-specific
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/feed",
    # general business / markets
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://finance.yahoo.com/news/rssindex",
    # tech / AI
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    # oil / energy
    "https://oilprice.com/rss/main",
    # world news
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

KEYWORD_GROUPS = {
    "crypto": ["crypto", "bitcoin", "ethereum", "coin", "token", "stablecoin", "memecoin", "defi", "sec crypto", "solana", "xrp"],
    "ai": ["openai", "chatgpt", "anthropic", "claude ai", "artificial intelligence", " ai model", "nvidia", "gpu chip", "deepseek", "gemini"],
    "big_tech": ["apple", "google", "microsoft", "amazon", "meta platforms", "tesla", "nvidia", "alphabet"],
    "oil_energy": ["opec", "oil price", "crude", "pipeline", "oil supply", "natural gas", "strait of hormuz"],
    "fed_rates": ["federal reserve", "interest rate", "fed chair", "rate cut", "rate hike", "powell", "fomc"],
    "market_moving": ["earnings beat", "earnings miss", "stock plunge", "stock surge", "market sell-off", "recession", "inflation report", "jobs report", "gdp growth", "bankruptcy", "ipo"],
    "geopolitics": ["sanctions", "military strike", "invasion", "ceasefire", "trade war", "tariffs", "war"],
    "trump_crypto": {"a": ["trump"], "b": ["coin", "token", "crypto", "bitcoin", "memecoin"]},
    "trump_geopolitics": {"a": ["trump"], "b": ["iran", "strike", "attack", "military", "sanctions", "israel", "china tariff"]},
}

# Fallback map used only when the AI call fails. Direction is unknown here.
IMPACT = {
    "crypto": "COIN, MSTR, MARA, RIOT, IBIT",
    "ai": "NVDA, AMD, MSFT, GOOGL, SMCI",
    "big_tech": "AAPL, MSFT, GOOGL, AMZN, META, TSLA",
    "oil_energy": "XOM, CVX, OXY (producers); DAL, UAL (fuel costs)",
    "fed_rates": "JPM, BAC, QQQ, XHB",
    "market_moving": "SPY, QQQ",
    "geopolitics": "LMT, RTX, NOC, XOM, CVX, GLD",
    "trump_crypto": "COIN, MSTR, DJT",
    "trump_geopolitics": "LMT, RTX, XOM, CVX, GLD",
}


def matches(text, group_def):
    text_l = text.lower()
    if isinstance(group_def, dict):
        return any(kw in text_l for kw in group_def["a"]) and any(kw in text_l for kw in group_def["b"])
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
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-500:], f)


def analyze_batch(hits):
    """One AI call for the whole digest. Returns a dict or None."""
    if not GITHUB_TOKEN:
        return None
    lines = [
        f"{i}. {h['title']} - {h['summary'][:150]}"
        for i, h in enumerate(hits, 1)
    ]
    prompt = (
        "You help a retail trader scan news. Based ONLY on these headlines, reply "
        "with ONLY JSON, no other text, in this shape: "
        '{"up": [{"ticker": "XOM", "why": "short reason"}], '
        '"down": [{"ticker": "DAL", "why": "short reason"}], '
        '"affected": [{"ticker": "COIN", "why": "short reason"}]}. '
        '"up" = stocks likely to rise, "down" = likely to fall, "affected" = '
        "likely to move but direction unclear. Max 5 per list, only real tickers, "
        "empty lists if nothing is clear. Be conservative.\n\n" + "\n".join(lines)
    )
    try:
        r = requests.post(
            "https://models.github.ai/inference/chat/completions",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 700,
            },
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"AI analysis failed: {e}")
        return None


def build_message(hits, analysis):
    parts = [f"{i}. {h['title']}\n{h['link']}" for i, h in enumerate(hits, 1)]
    text = "\n\n".join(parts)

    if analysis:
        sections = [
            ("📈 May go up", "up"),
            ("📉 May go down", "down"),
            ("↔️ Affected (direction unclear)", "affected"),
        ]
        for label, key in sections:
            items = [x for x in (analysis.get(key) or []) if isinstance(x, dict)]
            if items:
                text += f"\n\n{label}\n" + "\n".join(
                    f"{x.get('ticker', '?')} - {x.get('why', '')}" for x in items[:5]
                )
    else:
        groups = []
        for h in hits:
            if h["group"] not in groups:
                groups.append(h["group"])
        text += "\n\n↔️ Possibly affected (direction unknown)\n" + "\n".join(
            f"{g.replace('_', ' ').title()}: {IMPACT.get(g, 'n/a')}" for g in groups
        )

    text = text[:MAX_MESSAGE_CHARS]
    return text + "\n\nNot advice - verify before acting"


def send_notification(title, message):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
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
    hits = []
    hit_links = set()

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

            matched_group = None
            for group_name, keywords in KEYWORD_GROUPS.items():
                if matches(full_text, keywords):
                    matched_group = group_name
                    break

            if matched_group:
                if link not in hit_links:
                    hits.append({"title": title, "summary": summary, "link": link, "group": matched_group})
                    hit_links.add(link)
            else:
                new_seen.add(link)

    batch = hits[:MAX_HEADLINES]  # overflow stays unseen and goes out next run
    if batch:
        analysis = analyze_batch(batch)
        message = build_message(batch, analysis)
        send_notification(f"🚨 Market digest ({len(batch)} headlines)", message)
        print(f"Sent digest with {len(batch)} headlines (AI: {'yes' if analysis else 'fallback'})")
        for h in batch:
            new_seen.add(h["link"])

    save_seen(new_seen)


if __name__ == "__main__":
    main()
