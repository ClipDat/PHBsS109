"""
Memecoin spotter: scans trending/new pools (GeckoTerminal, free, no key) and
alerts on ones with real volume, buyer dominance, and a live price move.
Finds MOMENTUM, not winners. Most new memecoins go to zero. Not advice.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "changeme-topic")
SEEN_FILE = "seen_coins.json"

NETWORKS = ["solana", "base", "eth", "bsc"]

# Tune these. Stricter = fewer alerts, fewer junk coins.
MIN_LIQUIDITY = 20_000      # USD in the pool (low liquidity = easy to rug)
MIN_H1_VOLUME = 20_000      # USD traded in the last hour
MIN_H1_CHANGE = 15          # % price change in the last hour
MAX_H1_CHANGE = 400         # skip ones that already exploded
MIN_AGE_H = 0.5             # hours (skip brand-new, often manipulated)
MAX_AGE_H = 72              # hours (focus on fresh coins)
MIN_BUY_SELL_RATIO = 1.3    # buys must clearly outnumber sells
MAX_ALERTS = 5

if not os.path.exists(SEEN_FILE):
    with open(SEEN_FILE, "w") as f:
        json.dump([], f)


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-1000:], f)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def age_hours(created):
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def fetch(network, kind):
    r = requests.get(
        f"https://api.geckoterminal.com/api/v2/networks/{network}/{kind}",
        headers={"Accept": "application/json;version=20230302"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def evaluate(pool, network):
    a = pool.get("attributes") or {}
    liq = num(a.get("reserve_in_usd"))
    h1_vol = num((a.get("volume_usd") or {}).get("h1"))
    h1_chg = num((a.get("price_change_percentage") or {}).get("h1"))
    tx = ((a.get("transactions") or {}).get("h1")) or {}
    buys, sells = num(tx.get("buys")), num(tx.get("sells"))
    age = age_hours(a.get("pool_created_at"))

    if age is None or age < MIN_AGE_H or age > MAX_AGE_H:
        return None
    if liq < MIN_LIQUIDITY or h1_vol < MIN_H1_VOLUME:
        return None
    if h1_chg < MIN_H1_CHANGE or h1_chg > MAX_H1_CHANGE:
        return None
    if buys < MIN_BUY_SELL_RATIO * max(sells, 1):
        return None

    score = (h1_vol / liq) * 2 + buys / max(sells, 1) + min(h1_chg, 100) / 50
    addr = a.get("address")
    return {
        "addr": addr,
        "name": a.get("name", "?"),
        "network": network,
        "age": age,
        "liq": liq,
        "vol": h1_vol,
        "chg": h1_chg,
        "buys": buys,
        "sells": sells,
        "score": score,
        "link": f"https://www.geckoterminal.com/{network}/pools/{addr}",
    }


def send(title, message):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": "high", "Tags": "rocket"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Failed to send notification: {e}")


def main():
    seen = load_seen()
    found = {}

    for network in NETWORKS:
        for kind in ("trending_pools", "new_pools"):
            try:
                pools = fetch(network, kind)
            except Exception as e:
                print(f"Fetch failed {network}/{kind}: {e}")
                continue
            time.sleep(2)  # stay under the free rate limit
            for p in pools:
                addr = (p.get("attributes") or {}).get("address")
                if not addr or addr in seen or addr in found:
                    continue
                res = evaluate(p, network)
                if res:
                    found[addr] = res

    top = sorted(found.values(), key=lambda x: x["score"], reverse=True)[:MAX_ALERTS]
    if top:
        blocks = []
        for i, t in enumerate(top, 1):
            blocks.append(
                f"{i}. {t['name']} ({t['network'].upper()})\n"
                f"Age {t['age']:.1f}h | Liq ${t['liq']:,.0f} | 1h vol ${t['vol']:,.0f}\n"
                f"1h price {t['chg']:+.0f}% | buys/sells {int(t['buys'])}/{int(t['sells'])}\n"
                f"{t['link']}"
            )
            seen.add(t["addr"])
        msg = "\n\n".join(blocks) + (
            "\n\nMomentum only. Most new memecoins go to zero. "
            "Check liquidity lock, holders and contract first. Not advice."
        )
        send(f"🚀 Memecoin spotter ({len(top)})", msg[:3600])
        print(f"Alerted {len(top)} coins")

    save_seen(seen)


if __name__ == "__main__":
    main()
