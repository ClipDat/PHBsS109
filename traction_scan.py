"""
traction_scan.py - finds days-old coins with real traction (GeckoTerminal, free)
and paper-trades each alert. Runs on GitHub Actions every ~20 min.
State: traction_state.json   Results: traction_trades.csv
Not financial advice. Most coins lose money.
"""

import csv
import json
import os
import time
from datetime import datetime, timezone

import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "changeme-topic")
STATE_FILE = "traction_state.json"
TRADES_FILE = "traction_trades.csv"
NETWORKS = ["solana", "base", "eth", "bsc"]
BASE = "https://api.geckoterminal.com/api/v2"

# ---------------- candidate filters (tune these) ----------------
MIN_AGE_H = 18
MAX_AGE_H = 168              # 7 days
MIN_LIQ_USD = 75_000
MAX_FDV_USD = 20_000_000
MIN_VOL24_USD = 150_000
MIN_VOL_LIQ = 0.5            # 24h volume / liquidity
MAX_VOL_LIQ = 10
MIN_TX24 = 300
MIN_BUYERS24 = 100
MIN_H24_CHG = 15             # percent
MAX_H24_CHG = 300
MIN_H6_CHG = 0
MIN_H1_CHG = -3
MIN_BUY_SELL_H6 = 1.05
MAX_ALERTS_PER_RUN = 5
MAX_OPEN = 15

# ---------------- paper trading ----------------
BET_USD = 1000
SIM_FEE = 0.01               # per side (DEX + platform fee), check real fees
SIM_TX_USD = 1.0             # network/priority fee per transaction
TAKE_PROFIT = 1.00           # +100% net of all costs
STOP_LOSS = 0.30
TRAIL_ARM = 0.40             # trailing stop turns on after +40%
TRAIL_DROP = 0.35            # sell if it falls 35% from its peak
MAX_HOLD_H = 72

HEADERS = {"Accept": "application/json;version=20230302"}


# ---------------- helpers ----------------
def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def age_hours(created):
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def get(url, params=None):
    for attempt in range(2):
        r = requests.get(url, headers=HEADERS, params=params, timeout=25)
        if r.status_code == 429 and attempt == 0:
            time.sleep(25)
            continue
        r.raise_for_status()
        return r.json()


def pace():
    time.sleep(2.2)   # free limit is 30 calls/min


def parse_pool(p, network):
    a = p.get("attributes") or {}
    tx = a.get("transactions") or {}
    t6, t24 = tx.get("h6") or {}, tx.get("h24") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    return {
        "network": network,
        "addr": a.get("address"),
        "name": a.get("name") or "?",
        "price": num(a.get("base_token_price_usd")),
        "liq": num(a.get("reserve_in_usd")),
        "fdv": num(a.get("fdv_usd")),
        "age_h": age_hours(a.get("pool_created_at")),
        "vol24": num(vol.get("h24")),
        "c1": num(chg.get("h1")), "c6": num(chg.get("h6")), "c24": num(chg.get("h24")),
        "tx24": num(t24.get("buys")) + num(t24.get("sells")),
        "buyers24": num(t24.get("buyers")),
        "buys6": num(t6.get("buys")), "sells6": num(t6.get("sells")),
    }


def reject_reason(c):
    if not c["addr"] or c["price"] <= 0:
        return "no_data"
    if c["age_h"] is None or not (MIN_AGE_H <= c["age_h"] <= MAX_AGE_H):
        return "age"
    if c["liq"] < MIN_LIQ_USD:
        return "liquidity"
    if c["fdv"] > MAX_FDV_USD:
        return "fdv"
    if c["vol24"] < MIN_VOL24_USD:
        return "volume"
    if not (MIN_VOL_LIQ <= c["vol24"] / c["liq"] <= MAX_VOL_LIQ):
        return "vol_liq"
    if c["tx24"] < MIN_TX24 or c["buyers24"] < MIN_BUYERS24:
        return "activity"
    if not (MIN_H24_CHG <= c["c24"] <= MAX_H24_CHG):
        return "h24_change"
    if c["c6"] < MIN_H6_CHG or c["c1"] < MIN_H1_CHG:
        return "momentum"
    if c["buys6"] < MIN_BUY_SELL_H6 * max(c["sells6"], 1):
        return "buy_pressure"
    return None


# ---------------- state ----------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("seen", {})
    s.setdefault("open", {})
    s.setdefault("totals", {"n": 0, "wins": 0, "pnl": 0.0})
    return s


def save_state(s):
    cutoff = time.time() - 14 * 86400
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


def ensure_csv():
    if not os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["entry_time", "exit_time", "network", "name", "pool",
                                    "entry_price", "exit_price", "reason", "hold_h",
                                    "peak_multiple", "pnl_usd", "pnl_pct"])


# ---------------- paper trading ----------------
def impact(liq):
    return min(0.5, 2 * BET_USD / max(liq, 1))   # rough price impact of the order


def open_position(c, now):
    entry_eff = c["price"] * (1 + impact(c["liq"]))
    return {
        "addr": c["addr"], "network": c["network"], "name": c["name"],
        "entry_t": now, "entry_price": c["price"],
        "tokens": BET_USD * (1 - SIM_FEE) / entry_eff,
        "invested": BET_USD + SIM_TX_USD,
        "peak_ratio": 1.0, "last_ratio": 1.0, "last_price": c["price"], "miss": 0,
    }


def position_ratio(pos, price, liq):
    value = pos["tokens"] * price * (1 - impact(liq)) * (1 - SIM_FEE) - SIM_TX_USD
    return value / pos["invested"]


def exit_reason(pos, ratio, now):
    pos["peak_ratio"] = max(pos["peak_ratio"], ratio)
    if ratio >= 1 + TAKE_PROFIT:
        return "take_profit"
    if ratio <= 1 - STOP_LOSS:
        return "stop_loss"
    if pos["peak_ratio"] >= 1 + TRAIL_ARM and ratio <= pos["peak_ratio"] * (1 - TRAIL_DROP):
        return "trailing"
    if now - pos["entry_t"] >= MAX_HOLD_H * 3600:
        return "timeout"
    return None


def close_position(state, pos, reason, ratio, price, now):
    pnl = ratio * pos["invested"] - pos["invested"]
    t = state["totals"]
    t["n"] += 1
    t["wins"] += 1 if pnl > 0 else 0
    t["pnl"] += pnl
    ensure_csv()
    with open(TRADES_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(pos["entry_t"])),
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
            pos["network"], pos["name"], pos["addr"], pos["entry_price"], price, reason,
            round((now - pos["entry_t"]) / 3600, 1), round(pos["peak_ratio"], 2),
            round(pnl, 2), round(pnl / pos["invested"] * 100, 1)])
    state["open"].pop(pos["addr"], None)
    print(f"SIM closed {pos['name']} ({reason}): {pnl:+.0f} USD")


def refresh_open(state, now):
    by_net = {}
    for pos in state["open"].values():
        by_net.setdefault(pos["network"], []).append(pos["addr"])
    for net, addrs in by_net.items():
        found = {}
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            try:
                data = get(f"{BASE}/networks/{net}/pools/multi/{','.join(chunk)}")
                pace()
            except Exception as e:
                print(f"Refresh failed {net}: {e}")
                continue
            for p in data.get("data", []):
                c = parse_pool(p, net)
                if c["addr"]:
                    found[c["addr"]] = c
        for addr in addrs:
            pos = state["open"].get(addr)
            if not pos:
                continue
            c = found.get(addr)
            if not c or c["price"] <= 0:
                pos["miss"] += 1
                if pos["miss"] >= 4:
                    close_position(state, pos, "no_data", pos["last_ratio"], pos["last_price"], now)
                continue
            pos["miss"] = 0
            ratio = position_ratio(pos, c["price"], c["liq"])
            pos["last_ratio"], pos["last_price"] = ratio, c["price"]
            reason = exit_reason(pos, ratio, now)
            if reason:
                close_position(state, pos, reason, ratio, c["price"], now)


# ---------------- alerts ----------------
def send(title, message):
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Priority": "default",
                               "Tags": "chart_with_upwards_trend"}, timeout=10)
    except requests.RequestException as e:
        print(f"ntfy failed: {e}")


def main():
    state = load_state()
    ensure_csv()
    now = time.time()
    refresh_open(state, now)

    cands, rejects = {}, {}
    for net in NETWORKS:
        for path, params in ((f"networks/{net}/trending_pools", None),
                             (f"networks/{net}/pools", {"order": "h24_volume_usd_desc", "page": 1})):
            try:
                data = get(f"{BASE}/{path}", params)
            except Exception as e:
                print(f"Fetch failed {net}/{path}: {e}")
                continue
            pace()
            for p in data.get("data", []):
                c = parse_pool(p, net)
                if not c["addr"] or c["addr"] in state["seen"] or c["addr"] in state["open"]:
                    continue
                reason = reject_reason(c)
                if reason:
                    rejects[reason] = rejects.get(reason, 0) + 1
                else:
                    cands[c["addr"]] = c

    top = sorted(cands.values(), key=lambda c: c["vol24"], reverse=True)[:MAX_ALERTS_PER_RUN]
    lines = []
    for i, c in enumerate(top, 1):
        state["seen"][c["addr"]] = now
        opened = len(state["open"]) < MAX_OPEN
        if opened:
            state["open"][c["addr"]] = open_position(c, now)
        lines.append(
            f"{i}. {c['name']} ({c['network'].upper()}) | age {c['age_h']:.0f}h\n"
            f"Liq ${c['liq']:,.0f} | FDV ${c['fdv']:,.0f} | vol24 ${c['vol24']:,.0f}\n"
            f"Change 1h {c['c1']:+.0f}% | 6h {c['c6']:+.0f}% | 24h {c['c24']:+.0f}% | "
            f"{int(c['buyers24'])} buyers\n"
            f"https://www.geckoterminal.com/{c['network']}/pools/{c['addr']}"
            f"{'' if opened else '  (sim full - not tracked)'}")

    t = state["totals"]
    wr = f"{t['wins'] / t['n'] * 100:.0f}%" if t["n"] else "-"
    summary = (f"Sim: {t['n']} closed | win rate {wr} | P&L ${t['pnl']:+,.0f} | "
               f"{len(state['open'])} open")
    print(summary)
    print("Rejected:", rejects, "| passed:", len(cands))

    if lines:
        send(f"📈 Traction coins ({len(lines)})",
             "\n\n".join(lines) + f"\n\n{summary}\n"
             "Check holders/contract (e.g. RugCheck) before buying. Not advice.")
    save_state(state)


if __name__ == "__main__":
    main()
