"""
News watcher with AI analysis.

Every run (about every 15 min on GitHub Actions) it:
  1. reads the RSS feeds and picks NEW headlines that match your keyword groups,
  2. looks up the last-session move of the stocks/coins they could affect (Yahoo Finance, free),
  3. asks an AI (GitHub Models, free) which assets each headline could move, in which direction,
     how confident it is, and whether the news is probably priced in already,
  4. sends ONE phone notification (ntfy) with the headlines, links and the AI's lean per ticker,
  5. keeps a SCORECARD: every AI call is checked ~24 h later against the real price, so you can see
     whether its calls beat a coin flip before you trust them.

This is a reading aid, not advice. The AI only sees headlines and prices. It cannot know where a price
will go, and most headlines are already priced in within minutes.
"""

import csv
import html
import json
import os
import re
from datetime import datetime, timezone

import feedparser
import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "changeme-topic")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")      # optional, free from aistudio.google.com/apikey
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]   # tried in order
AI_MODEL = "openai/gpt-4o-mini"          # any model from the GitHub Models catalog
SEEN_FILE = "seen.json"
CALLS_FILE = "calls_log.json"
SCORE_FILE = "scorecard.csv"
MAX_HEADLINES = 8                         # per notification; extras wait for the next run
MAX_QUOTES = 24                           # price lookups per run
SCORE_AFTER_H = 24                        # check an AI call against the real price after this many hours

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/feed",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://finance.yahoo.com/news/rssindex",
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://oilprice.com/rss/main",
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

# Assets each topic could move (Yahoo Finance symbols). The AI may add others.
CANDIDATES = {
    "crypto": ["BTC-USD", "ETH-USD", "SOL-USD", "COIN", "MSTR"],
    "ai": ["NVDA", "AMD", "MSFT", "GOOGL", "SMCI"],
    "big_tech": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA"],
    "oil_energy": ["CL=F", "XOM", "CVX", "OXY", "DAL", "UAL"],
    "fed_rates": ["^TNX", "QQQ", "JPM", "BAC", "XHB"],
    "market_moving": ["SPY", "QQQ"],
    "geopolitics": ["CL=F", "GLD", "LMT", "RTX", "XOM"],
    "trump_crypto": ["BTC-USD", "COIN", "MSTR", "DJT"],
    "trump_geopolitics": ["CL=F", "GLD", "LMT", "RTX", "XOM"],
}


# ---------------------------------------------------------------- helpers
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def matches(text, group_def):
    t = text.lower()
    if isinstance(group_def, dict):
        return any(k in t for k in group_def["a"]) and any(k in t for k in group_def["b"])
    return any(k in t for k in group_def)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def ensure_files():
    for path, default in ((SEEN_FILE, []), (CALLS_FILE, [])):
        if not os.path.exists(path):
            write_json(path, default)
    if not os.path.exists(SCORE_FILE):
        with open(SCORE_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["time", "ticker", "lean", "confidence", "horizon", "move_pct", "correct"])


# ---------------------------------------------------------------- prices
def get_quote(ticker):
    """Last price and last-session change in %. None if Yahoo does not answer (it is unofficial and can block)."""
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                         params={"range": "5d", "interval": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        res = r.json()["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
        if not closes:
            return None
        price = closes[-1]
        chg = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else None
        return {"price": price, "chg": chg}
    except Exception:
        return None


def fetch_quotes(tickers, have=None):
    quotes = dict(have or {})
    for t in tickers:
        if t not in quotes and len(quotes) < MAX_QUOTES:
            quotes[t] = get_quote(t)
    return quotes


# ---------------------------------------------------------------- AI
AI_ERROR = ""


def ask_ai(hits, quotes):
    global AI_ERROR
    if not GITHUB_TOKEN and not GEMINI_API_KEY:
        AI_ERROR = "no AI key passed in - the workflow needs env GITHUB_TOKEN and/or GEMINI_API_KEY"
        return None
    today = datetime.now(timezone.utc).strftime("%A %Y-%m-%d")
    heads = "\n".join(f"{i}. [{h['group']}] {h['title']} -- {h['summary'][:220]}" for i, h in enumerate(hits, 1))
    px = {t: {"last": round(q["price"], 2), "last_session_change_pct": None if q["chg"] is None else round(q["chg"], 2)}
          for t, q in quotes.items() if q}
    prompt = (
        "You help a retail trader read the news. You are cautious. You do not give investment advice; you describe the "
        "LIKELY direction of impact and how sure you are.\n"
        f"Today is {today} (UTC). US stock markets are closed on weekends, so a stock's reaction comes at the next open; "
        "crypto trades 24/7.\n\n"
        f"Headlines:\n{heads}\n\nPrice context (last session): {json.dumps(px)}\n\n"
        "Reply with ONLY JSON, no other text, in this shape:\n"
        '{"overall": "one sentence", "items": [{"n": 1, "market_moving": true, "calls": [{"ticker": "XOM", '
        '"lean": "up|down|unclear", "horizon": "hours|days|weeks", "confidence": "low|medium|high", '
        '"priced_in": "yes|no|unknown", "why": "max 20 words"}]}]}\n'
        "Rules: set market_moving=false (and no calls) for routine, rumor-only, opinion or already widely known news. "
        "Use only real tickers (Yahoo symbols like BTC-USD, CL=F). Max 3 calls per headline. Prefer 'unclear' over guessing. "
        "Use confidence 'high' ONLY for hard, specific news (rate decisions, earnings numbers, sanctions, official actions). "
        "If the last-session change already moved a lot in the direction the news implies, say priced_in=yes."
    )
    errors = []

    def parse(text):
        text = text[text.index("{"): text.rindex("}") + 1]
        return clean_analysis(json.loads(text), len(hits))

    def attempt(name, fn):
        resp = None
        try:
            resp = fn()
            body = (resp.text or "").strip()
            if resp.status_code != 200 or not body:
                errors.append(f"{name} HTTP {resp.status_code}: {body[:120] or '(empty reply)'}")
                return None
            return parse(extract(name, resp))
        except Exception as e:
            shown = (resp.text or "")[:80] if resp is not None else ""
            errors.append(f"{name} {type(e).__name__}: {str(e)[:90]} | reply: {shown!r}")
            return None

    def extract(name, resp):
        d = resp.json()
        if name == "gemini":
            return d["candidates"][0]["content"]["parts"][0]["text"]
        return d["choices"][0]["message"]["content"]

    providers = []
    if GEMINI_API_KEY:
        for gm in GEMINI_MODELS:
            cfg = {"temperature": 0.2, "responseMimeType": "application/json", "maxOutputTokens": 1800}
            if gm.startswith("gemini-2.5"):
                cfg["thinkingConfig"] = {"thinkingBudget": 0}      # no 'thinking' = fast answer
            providers.append(("gemini", lambda gm=gm, cfg=cfg: requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{gm}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}, timeout=40)))
    if GITHUB_TOKEN:
        providers.append(("github", lambda: requests.post(
            "https://models.github.ai/inference/chat/completions",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
            json={"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2, "max_tokens": 1400}, timeout=60)))
    for name, fn in providers:
        result = attempt(name, fn)
        if result:
            AI_ERROR = ""
            return result
    AI_ERROR = " || ".join(errors) or "no AI key available"
    print("AI analysis failed:", AI_ERROR)
    return None


def clean_analysis(data, n_hits):
    """Keep only well-formed fields so one odd answer cannot break the message."""
    leans, confs, hors, pri = {"up", "down", "unclear"}, {"low", "medium", "high"}, {"hours", "days", "weeks"}, {"yes", "no", "unknown"}
    items = {}
    for it in data.get("items", []):
        try:
            n = int(it["n"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= n <= n_hits:
            continue
        calls = []
        if it.get("market_moving"):
            for c in (it.get("calls") or [])[:3]:
                t = str(c.get("ticker", "")).upper().strip()
                if not re.fullmatch(r"[A-Z0-9.\-=^]{1,12}", t):
                    continue
                calls.append(dict(ticker=t,
                                  lean=c.get("lean") if c.get("lean") in leans else "unclear",
                                  horizon=c.get("horizon") if c.get("horizon") in hors else "days",
                                  confidence=c.get("confidence") if c.get("confidence") in confs else "low",
                                  priced_in=c.get("priced_in") if c.get("priced_in") in pri else "unknown",
                                  why=clean(str(c.get("why", "")))[:140]))
        items[n] = dict(market_moving=bool(it.get("market_moving")), calls=calls)
    return dict(overall=clean(str(data.get("overall", "")))[:200], items=items)


# ---------------------------------------------------------------- scorecard
def log_calls(hits, analysis, quotes):
    log = read_json(CALLS_FILE, [])
    for n, it in analysis["items"].items():
        for c in it["calls"]:
            q = quotes.get(c["ticker"])
            if c["lean"] in ("up", "down") and q:
                log.append(dict(ts=now_iso(), ticker=c["ticker"], lean=c["lean"], confidence=c["confidence"],
                                horizon=c["horizon"], price0=q["price"], scored=False))
    write_json(CALLS_FILE, log[-400:])


def score_old_calls():
    """Check calls older than SCORE_AFTER_H against the real price now."""
    log = read_json(CALLS_FILE, [])
    cache, changed = {}, False
    now = datetime.now(timezone.utc)
    for c in log:
        if c.get("scored"):
            continue
        age_h = (now - datetime.fromisoformat(c["ts"])).total_seconds() / 3600
        if age_h < SCORE_AFTER_H:
            continue
        if c["ticker"] not in cache:
            cache[c["ticker"]] = get_quote(c["ticker"])
        q = cache[c["ticker"]]
        if not q:
            continue
        move = (q["price"] / c["price0"] - 1) * 100
        if abs(move) < 0.05 and age_h < 72:       # markets were closed; wait for a real move
            continue
        correct = (move > 0) == (c["lean"] == "up")
        with open(SCORE_FILE, "a", newline="") as f:
            csv.writer(f).writerow([c["ts"], c["ticker"], c["lean"], c["confidence"], c["horizon"], round(move, 2), int(correct)])
        c["scored"] = True
        changed = True
    if changed:
        write_json(CALLS_FILE, [c for c in log if not c.get("scored") or
                                (now - datetime.fromisoformat(c["ts"])).days < 7])


def scorecard_line():
    try:
        rows = list(csv.DictReader(open(SCORE_FILE)))
    except OSError:
        return ""
    if not rows:
        return "Scorecard: no AI call has been checked yet."
    n, ok = len(rows), sum(int(r["correct"]) for r in rows)
    out = f"Scorecard: {ok}/{n} AI calls were right ({ok / n * 100:.0f}%; a coin flip is 50%)"
    hi = [r for r in rows if r["confidence"] == "high"]
    if len(hi) >= 5:
        out += f" | high-confidence: {sum(int(r['correct']) for r in hi)}/{len(hi)}"
    return out


# ---------------------------------------------------------------- message
ARROW = {"up": "▲ UP", "down": "▼ DOWN", "unclear": "? unclear"}


def build_message(hits, analysis, quotes):
    parts, quiet = [], 0
    for i, h in enumerate(hits, 1):
        it = analysis["items"].get(i) if analysis else None
        if analysis and it and not it["market_moving"]:
            quiet += 1
            continue
        block = f"{i}. {h['title']}\n{h['link']}"
        if it and it["calls"]:
            for c in it["calls"]:
                q = quotes.get(c["ticker"])
                last = f" | last session {q['chg']:+.1f}%" if q and q["chg"] is not None else ""
                pin = " | likely priced in" if c["priced_in"] == "yes" else ""
                block += (f"\n   {c['ticker']} {ARROW[c['lean']]} ({c['confidence']}, {c['horizon']}){pin}{last}"
                          f"\n   {c['why']}")
        elif not analysis:
            groups = h["group"].replace("_", " ")
            block += f"\n   (AI unavailable) could affect: {', '.join(CANDIDATES.get(h['group'], [])[:5])} [{groups}]"
        parts.append(block)
    text = "\n\n".join(parts) if parts else "No market-moving headlines in this batch."
    if quiet:
        text += f"\n\n+{quiet} headline(s) judged not market-moving."
    if analysis and analysis["overall"]:
        text += f"\n\nOverall: {analysis['overall']}"
    if not analysis and AI_ERROR:
        text += f"\n\nAI error: {AI_ERROR}"
    text += f"\n\n{scorecard_line()}\nAI guess from headlines only, not advice. Check the price before acting."
    return text[:3700]


def send(title, message):
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Priority": "high", "Tags": "chart_with_upwards_trend"},
                      timeout=10)
    except requests.RequestException as e:
        print(f"Failed to send notification: {e}")


# ---------------------------------------------------------------- main
def main():
    ensure_files()
    seen = set(read_json(SEEN_FILE, []))
    new_seen = set(seen)
    hits, hit_links = [], set()
    for url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"Failed to parse {url}: {e}")
            continue
        for e in parsed.entries[:20]:
            link = e.get("link", "")
            if not link or link in seen:
                continue
            title, summary = clean(e.get("title", "")), clean(e.get("summary", ""))
            group = next((g for g, d in KEYWORD_GROUPS.items() if matches(f"{title} {summary}", d)), None)
            if group:
                if link not in hit_links:
                    hits.append(dict(title=title, summary=summary, link=link, group=group))
                    hit_links.add(link)
            else:
                new_seen.add(link)

    score_old_calls()

    batch = hits[:MAX_HEADLINES]
    if batch:
        cands = []
        for h in batch:
            for t in CANDIDATES.get(h["group"], []):
                if t not in cands:
                    cands.append(t)
        quotes = fetch_quotes(cands)
        analysis = ask_ai(batch, quotes)
        if analysis:
            extra = [c["ticker"] for it in analysis["items"].values() for c in it["calls"]]
            quotes = fetch_quotes(extra, quotes)
            log_calls(batch, analysis, quotes)
        send(f"📰 Market digest ({len(batch)})", build_message(batch, analysis, quotes))
        print(f"Sent digest with {len(batch)} headlines (AI: {'yes' if analysis else 'no'})")
        for h in batch:
            new_seen.add(h["link"])
    write_json(SEEN_FILE, list(new_seen)[-800:])


if __name__ == "__main__":
    main()
