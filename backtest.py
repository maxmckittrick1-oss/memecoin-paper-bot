"""
Backtester for the established-coin strategies.

Replays real one-minute price history for the coins in paper_bot.TOKENS and runs
every "core" strategy from paper_bot.py over it, using the same buy rules, exit
rules and position sizes as the live bot. A month of trading takes seconds
instead of a month.

Run it with:   python backtest.py            (last 30 days)
               python backtest.py 60         (last 60 days)

Price history is downloaded once from GeckoTerminal (free, no key) and cached in
the data/ folder; later runs only download the newest minutes.

Limits (read before trusting the numbers):
  - New-coin strategies can't be backtested: there's no reliable history for
    coins that launched and died within hours, and the safety checks can't be
    re-run for the past.
  - Trade costs use TODAY's real Jupiter quote for each coin, applied to the
    whole period.
  - Within a one-minute bar the order of high and low is unknown, so stops are
    assumed to hit before profit targets (the cautious assumption).
  - Past results don't predict future ones. A strategy that only wins in one
    half of the period probably just got lucky.
"""

import csv
import os
import sys
import time
from datetime import datetime, timezone

import paper_bot as pb

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DATA_DIR = os.path.join(pb.HERE, "data")
REPORT_FILE = os.path.join(pb.HERE, "backtest_report.txt")
GECKO_PAUSE_SECONDS = 2.5  # GeckoTerminal's free API allows about 30 requests a minute


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

def busiest_pool(mint):
    """The coin's pool with the most trading in the last 24h (best price history)."""
    pairs = pb.get_json("https://api.dexscreener.com/tokens/v1/solana/" + mint)
    pairs = [p for p in pairs if p["baseToken"]["address"] == mint]
    return max(pairs, key=lambda p: (p.get("volume") or {}).get("h24") or 0)["pairAddress"]


def gecko_candles(pool, before):
    """Up to 1000 one-minute candles ending before `before` (unix seconds), oldest first."""
    url = (f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
           f"?aggregate=1&limit=1000&currency=usd&token=base&before_timestamp={before}")
    for attempt in range(5):
        try:
            data = pb.get_json(url)
            rows = data["data"]["attributes"]["ohlcv_list"]
            return sorted((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                          for r in rows)
        except Exception as e:
            if "429" in str(e) and attempt < 4:
                time.sleep(20)  # rate limited - wait and retry
            else:
                raise
    return []


def load_history(symbol, mint):
    """{minute_timestamp: (open, high, low, close)} for the last DAYS days, cached on disk."""
    os.makedirs(DATA_DIR, exist_ok=True)
    pool = busiest_pool(mint)
    path = os.path.join(DATA_DIR, f"{symbol}_{pool}.csv")

    candles = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.reader(f):
                candles[int(r[0])] = tuple(map(float, r[1:5]))

    start = int(time.time()) - DAYS * 86400
    now_ts = int(time.time())
    newest = max(candles) if candles else 0
    oldest = min(candles) if candles else now_ts

    # Fetch newer minutes than we have, then older ones until DAYS is covered.
    if not candles:
        ranges = [(now_ts, start)]
    else:
        ranges = []
        if newest < now_ts - 120:
            ranges.append((now_ts, newest))
        if oldest > start:
            ranges.append((oldest, start))

    for before, stop_at in ranges:
        while before > stop_at:
            batch = gecko_candles(pool, before)
            time.sleep(GECKO_PAUSE_SECONDS)
            if not batch:
                break
            for ts, o, h, l, c in batch:
                candles[ts] = (o, h, l, c)
            got_to = batch[0][0]
            print(f"  {symbol}: have data back to "
                  f"{datetime.fromtimestamp(got_to, timezone.utc):%Y-%m-%d %H:%M} UTC")
            if got_to >= before:
                break
            before = got_to

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        for ts in sorted(candles):
            w.writerow([ts, *candles[ts]])

    return {ts: v for ts, v in candles.items() if ts >= start - 86400}  # +1 day for warm-up


def fill_gaps(candles, minutes):
    """Minutes with no trades get a flat candle at the last close."""
    before = [ts for ts in candles if ts < minutes[0]]
    last = candles[max(before)] if before else None
    if last is not None:
        last = (last[3],) * 4
    out = {}
    for ts in minutes:
        if ts in candles:
            last = candles[ts]
            out[ts] = last
        elif last is not None:
            c = last[3]
            out[ts] = (c, c, c, c)
    return out


# ---------------------------------------------------------------------------
# Trade costs: today's real Jupiter quote for a typical trade size
# ---------------------------------------------------------------------------

def measure_costs():
    """{mint: cost_pct per trade}, from a real Jupiter quote for a $250 buy."""
    prices = pb.fetch_fast_prices(list(pb.TOKENS.values()))
    costs = {}
    for sym, mint in pb.TOKENS.items():
        try:
            coins, fill = pb.simulate_buy(mint, 250, prices[mint], pb.FEE_PCT)
            pct = (1 - coins * prices[mint] / 250) * 100
            costs[mint] = max(pct, 0.05)
            print(f"  {sym}: {costs[mint]:.2f}% per trade ({fill})")
        except Exception:
            costs[mint] = pb.FEE_PCT
            print(f"  {sym}: {pb.FEE_PCT}% per trade (estimated)")
    return costs


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def run_strategy(name, strat, data, minutes, costs):
    cash = pb.STARTING_CASH
    positions = {}
    history = {m: [] for m in data}
    closes = {m: [] for m in data}
    trades = wins = 0
    peak_value, max_drawdown = cash, 0.0
    mid_ts = minutes[len(minutes) // 2]
    value_at_mid = None
    size_pct = strat.get("core_pct", pb.POSITION_PCT)
    bought_once = set()

    def sell(mint, fraction, price):
        nonlocal cash, trades, wins
        pos = positions[mint]
        coins, cost = pos["coins"] * fraction, pos["cost"] * fraction
        usd = coins * price * (1 - costs[mint] / 100)
        cash += usd
        trades += 1
        wins += usd > cost
        if fraction >= 1:
            del positions[mint]
        else:
            pos["coins"] -= coins
            pos["cost"] -= cost

    for ts in minutes:
        for mint, candles in data.items():
            if ts not in candles:
                continue
            o, h, l, c = candles[ts]

            # Exits first, using the bar's low and high.
            pos = positions.get(mint)
            if pos:
                style = strat["exit"]
                if style == "fixed":
                    stop = pos["entry"] * (1 - pb.STOP_LOSS_PCT / 100)
                    target = pos["entry"] * (1 + pb.TAKE_PROFIT_PCT / 100)
                    if l <= stop:
                        sell(mint, 1, min(o, stop))
                    elif h >= target:
                        sell(mint, 1, max(o, target))
                elif style in ("trail", "moonbag"):
                    level = pos["peak"] * (1 - pb.TRAIL_PCT / 100)
                    if l <= level:
                        sell(mint, 1, min(o, level))
                    else:
                        double = pos["entry"] * pb.MOONBAG_AT_X
                        if style == "moonbag" and not pos.get("half_sold") and h >= double:
                            sell(mint, pb.MOONBAG_SELL_PCT / 100, max(o, double))
                            pos["half_sold"] = True
                        pos["peak"] = max(pos["peak"], h)

            # Same inputs the live bot sees: % change over 5 min / 1 hour, and a
            # price history sampled every 30 seconds (two samples per minute).
            closes[mint].append(c)
            closes[mint] = closes[mint][-61:]
            history[mint].extend((c, c))
            history[mint] = history[mint][-100:]
            past = closes[mint]
            m = {
                "price": c,
                "m5": (c / past[-6] - 1) * 100 if len(past) > 5 else 0,
                "h1": (c / past[0] - 1) * 100 if len(past) > 60 else 0,
                "liquidity": 1e9,
            }

            signal_exit = strat.get("signal_exit")
            if mint in positions and signal_exit and signal_exit(mint, m, history):
                sell(mint, 1, c)

            if mint not in positions and not (name == "hodl" and mint in bought_once):
                if strat["buy"](mint, m, history):
                    usd = min(pb.STARTING_CASH * size_pct / 100, cash)
                    if usd >= 1:
                        cash -= usd
                        coins = usd * (1 - costs[mint] / 100) / c
                        positions[mint] = {"coins": coins, "cost": usd, "entry": c, "peak": c}
                        bought_once.add(mint)

        value = cash + sum(p["coins"] * data[mt][ts][3]
                           for mt, p in positions.items() if ts in data[mt])
        peak_value = max(peak_value, value)
        max_drawdown = max(max_drawdown, (1 - value / peak_value) * 100)
        if ts == mid_ts:
            value_at_mid = value

    return {
        "value": value,
        "trades": trades,
        "open": len(positions),
        "wins": wins,
        "max_drawdown": max_drawdown,
        "first_half": (value_at_mid / pb.STARTING_CASH - 1) * 100,
        "second_half": (value / value_at_mid - 1) * 100,
    }


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    print(f"Loading {DAYS} days of one-minute prices (first run takes a few minutes)...")
    raw = {mint: load_history(sym, mint) for sym, mint in pb.TOKENS.items()}
    # The period every coin has data for, and no longer than DAYS.
    start = max(max(min(c) for c in raw.values()), int(time.time()) - DAYS * 86400)
    start += 60 - start % 60 if start % 60 else 0
    end = min(max(c) for c in raw.values())
    minutes = list(range(start - start % 60, end + 1, 60))
    data = {mint: fill_gaps(c, minutes) for mint, c in raw.items()}

    print("\nMeasuring today's real trade costs from Jupiter...")
    costs = measure_costs()

    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    fmt = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
    out(f"\nBacktest {fmt(minutes[0])} -> {fmt(minutes[-1])} UTC "
        f"({len(minutes) / 1440:.1f} days), ${pb.STARTING_CASH:,.0f} per strategy")

    out("\nHow each coin did over the period (buy at start, hold to end):")
    for sym, mint in pb.TOKENS.items():
        first, last = data[mint][minutes[0]][3], data[mint][minutes[-1]][3]
        out(f"  {sym:<9} {(last / first - 1) * 100:+7.1f}%")

    out(f"\n{'strategy':<17} {'end value':>10} {'return':>8} {'trades':>7} {'open':>5} {'win %':>6} "
        f"{'worst drop':>10} {'1st half':>9} {'2nd half':>9}")
    results = {}
    for name, strat in pb.STRATEGIES.items():
        if strat["universe"] != "core":
            continue
        r = run_strategy(name, strat, data, minutes, costs)
        results[name] = r
    for name, r in sorted(results.items(), key=lambda kv: -kv[1]["value"]):
        win = f"{r['wins'] / r['trades'] * 100:.0f}" if r["trades"] else "-"
        out(f"{name:<17} ${r['value']:>9,.2f} {(r['value'] / pb.STARTING_CASH - 1) * 100:>+7.1f}% "
            f"{r['trades']:>7} {r['open']:>5} {win:>6} {r['max_drawdown']:>9.1f}% "
            f"{r['first_half']:>+8.1f}% {r['second_half']:>+8.1f}%")

    out("\n'trades' = completed sells; 'open' = positions still held at the end (valued at the")
    out("last price). 'worst drop' = biggest fall from a high point along the way.")
    out("A strategy worth trusting beats hodl in BOTH halves, not just overall.")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nSaved to {os.path.basename(REPORT_FILE)}")


if __name__ == "__main__":
    main()
