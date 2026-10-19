"""
Memecoin paper-trading bot (fake money only).

What it does:
  - Every FAST_SECONDS it gets live prices from Jupiter (free, no key) and
    checks every open position against its exit rules (stop-loss, take-profit,
    trailing stop). This is the "safety" loop, so it runs often.
  - Every SLOW_SECONDS it gets fuller market data from DexScreener (5-min and
    1-hour price change, liquidity, buys/sells) and decides whether to buy.
  - Every SCAN_SECONDS it looks for newly launched Solana coins (DexScreener,
    plus pump.fun graduations via Helius), safety-checks each one straight from
    the blockchain, and prints why it passed or failed.
  - Every pretend trade is priced with a real Jupiter quote for that size.
  - Wallet values are written to equity.csv every 15 minutes.
  - Several strategies run side by side, each with its own pretend wallet
    of STARTING_CASH dollars, so you can compare them on the same prices.
    "Core" strategies trade established coins (TOKENS); "new" strategies
    trade freshly launched coins found by the scanner.
  - Trades are logged to trades.csv; wallets are saved to state.json so you can
    stop the bot (Ctrl+C) and start it again later without losing progress.

No wallet, no private keys, no real orders. Nothing here can spend real money.

Run it with:   run_bot.bat   (restarts the bot automatically if it ever stops)
          or:  python paper_bot.py
Start fresh:   python paper_bot.py --reset
"""

import csv
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# Settings - change these to experiment
# ---------------------------------------------------------------------------

# Established coins to watch, by their Solana token address (safer than
# searching by name, because scammers launch fake copies with the same name).
TOKENS = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "FARTCOIN": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
    "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
    "MEW": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
}

STARTING_CASH = 1000.0   # pretend dollars per strategy
FAST_SECONDS = 3         # how often to check prices for exits (stops)
SLOW_SECONDS = 30        # how often to look for new buys
SCAN_SECONDS = 60        # how often to look for newly launched coins

# Trade costs. Each pretend trade asks Jupiter for a real quote: exactly how many
# coins (or dollars) that trade size would get right now, including pool fees and
# price impact. FEE_PCT / NEW_FEE_PCT are only used if a quote isn't available.
USE_JUPITER_QUOTES = True
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NETWORK_FEE_USD = 0.01   # Solana transaction + priority fee per swap (rough)
QUOTE_MIN_GAP_SECONDS = 3.0  # Jupiter's free tier allows about one quote every 2-3 seconds

# Established coins
FEE_PCT = 1.0            # fallback % lost per buy and per sell
POSITION_PCT = 25.0      # % of a wallet's starting cash put into each trade
MIN_LIQUIDITY_USD = 50_000  # skip coins with thin liquidity

# Newly launched coins: smaller bets, much worse slippage
NEW_FEE_PCT = 5.0        # fallback % lost per buy and per sell - tiny pools move a lot
NEW_POSITION_PCT = 5.0   # % of starting cash per new-coin trade ($50 of $1000)
NEW_MAX_AGE_HOURS = 24   # only coins launched in the last day count as "new"
NEW_WATCH_HOURS = 6      # stop watching a new coin after this long, unless we hold it
NEW_MAX_WATCHED = 25     # cap on how many new coins are watched at once
NEW_MAX_CHECKS_PER_SCAN = 5  # RugCheck lookups per scan (be polite to the free API)

# Safety checks for new coins (RugCheck). A coin must pass ALL of these.
# (Checked straight from the blockchain via Helius - RugCheck shut down on
# 2026-10-18. Not covered any more: insider-wallet networks and holder count.)
SAFE_MIN_LIQUIDITY_USD = 15_000   # pool big enough to sell into
SAFE_MAX_TOP10_PCT = 30           # top 10 wallets (excluding pools) own at most this %
SAFE_MAX_ONE_HOLDER_PCT = 10      # no single wallet owns more than this %
# Also required: a pump.fun coin that has graduated (its liquidity is locked by
# design), no mint authority, no freeze authority, no transfer tax.

# Exit rules
STOP_LOSS_PCT = 8.0      # "fixed" exit: sell if a position falls this much
TAKE_PROFIT_PCT = 15.0   # "fixed" exit: sell if a position rises this much
TRAIL_PCT = 25.0         # "trail"/"moonbag" exit: sell if price falls this much from its peak
NEW_TRAIL_PCT = 30.0     # wider trailing stop for new coins, which swing harder
MOONBAG_AT_X = 2.0       # "moonbag" exit: when price reaches this multiple of entry...
MOONBAG_SELL_PCT = 50.0  # ...sell this % of the position, then trail the rest

# Combined wallet ("combo" strategy): one wallet trading both kinds of coin
COMBO_CORE_POSITION_PCT = 13.0  # % per established coin (6 coins x 13% = 78% max)
COMBO_NEW_MAX_PCT = 20.0        # never more than this % of the wallet in new coins at once

# "ma_trend" strategy: ignore crossovers smaller than this %, since a round trip
# costs 2x FEE_PCT and smaller moves can't cover it.
MA_MIN_GAP_PCT = 1.0

# If the bot was off longer than this (crash, PC off), the restart is logged and
# sells in the first GAP_SELL_SECONDS are labelled "gap sell", because they happen
# at prices from long after the stop should have triggered.
RESTART_GAP_SECONDS = 120
GAP_SELL_SECONDS = 60

# If Jupiter can't find ANY way to sell a coin (pool pulled, or a coin mid-move to
# a new pool), keep retrying every minute; after this long, count it as worth $0.
NO_ROUTE_WRITE_OFF_SECONDS = 600

# Value of every wallet is written to equity.csv this often, so the analysis can
# show how each strategy's money moved over time (not just where it ended).
EQUITY_SNAPSHOT_SECONDS = 900

# Helius (optional): set the HELIUS_API_KEY environment variable to your free
# Helius API key and the scanner also picks up pump.fun coins the moment they
# "graduate" to a real trading pool, and can safety-check new coins. Without it
# the bot still trades the established coins. The key is never stored in code.
HELIUS_KEY_ENV = "HELIUS_API_KEY"
PUMP_MIGRATION_ACCOUNT = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
HELIUS_MAX_TX_PER_SCAN = 10  # keeps usage well inside Helius's free plan
GRADUATE_RETRY_MINUTES = 10  # wait this long for a graduate to appear on DexScreener

# "new_pullback" strategy: after a coin runs up at least PULLBACK_RUNUP_PCT, buy
# when it has dipped PULLBACK_MIN..MAX_PCT below its recent high and buyers still
# outnumber sellers - instead of buying the spike itself.
PULLBACK_RUNUP_PCT = 30.0
PULLBACK_MIN_PCT = 15.0
PULLBACK_MAX_PCT = 35.0

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
TRADES_FILE = os.path.join(HERE, "trades.csv")
SCAN_FILE = os.path.join(HERE, "scanned_coins.csv")
EQUITY_FILE = os.path.join(HERE, "equity.csv")


def load_dotenv(path=os.path.join(HERE, ".env")):
    """Read KEY=value lines from a local .env file (kept out of git) into the
    environment. Variables that are already set win."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"'))
    except OSError:
        pass


load_dotenv()


# ---------------------------------------------------------------------------
# Price and market data
# ---------------------------------------------------------------------------

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def chunks(xs, n):
    xs = list(xs)
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def fetch_market(mints):
    """DexScreener: {mint: {...}} from each coin's most liquid pool."""
    best = {}
    for batch in chunks(mints, 30):
        pairs = get_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch))
        for pair in pairs:
            mint = pair["baseToken"]["address"]
            if mint not in batch or not pair.get("priceUsd"):
                continue
            liquidity = (pair.get("liquidity") or {}).get("usd") or 0
            if mint in best and liquidity <= best[mint]["liquidity"]:
                continue
            change = pair.get("priceChange") or {}
            txns = (pair.get("txns") or {}).get("m5") or {}
            created = pair.get("pairCreatedAt") or 0
            best[mint] = {
                "symbol": pair["baseToken"].get("symbol", "?"),
                "dex": pair.get("dexId", ""),
                "price": float(pair["priceUsd"]),
                "m5": change.get("m5", 0) or 0,
                "h1": change.get("h1", 0) or 0,
                "h24": change.get("h24", 0) or 0,
                "liquidity": liquidity,
                "buys5m": txns.get("buys", 0) or 0,
                "sells5m": txns.get("sells", 0) or 0,
                "age_hours": (time.time() * 1000 - created) / 3_600_000 if created else 9999,
            }
    return best


DECIMALS = {}  # mint -> token decimals, learned from Jupiter (needed to quote sells)


def fetch_fast_prices(mints):
    """Jupiter: {mint: price}. Updates more often than DexScreener, so it's used for stops."""
    prices = {}
    for batch in chunks(mints, 50):
        data = get_json("https://lite-api.jup.ag/price/v3?ids=" + ",".join(batch))
        for mint in batch:
            if mint in data and data[mint].get("usdPrice"):
                prices[mint] = float(data[mint]["usdPrice"])
                if data[mint].get("decimals") is not None:
                    DECIMALS[mint] = int(data[mint]["decimals"])
    return prices


_quote_cache = {}
_last_quote_time = 0.0


def jupiter_quote(input_mint, output_mint, amount_raw):
    """How much of output_mint a swap of amount_raw (smallest units) gets right now."""
    global _last_quote_time
    key = (input_mint, output_mint, amount_raw)
    hit = _quote_cache.get(key)
    if hit and time.time() - hit[0] < 10:
        return hit[1]  # several strategies buying the same coin at once share a quote
    url = ("https://lite-api.jup.ag/swap/v1/quote"
           f"?inputMint={input_mint}&outputMint={output_mint}"
           f"&amount={amount_raw}&slippageBps=1000")
    for attempt in range(2):
        wait = QUOTE_MIN_GAP_SECONDS - (time.time() - _last_quote_time)
        if wait > 0:
            time.sleep(wait)
        _last_quote_time = time.time()
        try:
            data = get_json(url)
            break
        except Exception as e:
            if "429" in str(e) and attempt == 0:
                time.sleep(5)  # rate limited: wait a moment and try once more
                continue
            raise
    out = int(data["outAmount"])
    if len(_quote_cache) > 500:
        _quote_cache.clear()
    _quote_cache[key] = (time.time(), out)
    return out


_recent_cost = {}  # (mint, "buy"/"sell") -> (time, cost %) from the last real quote


def fallback_cost(mint, side, fee_pct):
    """No quote available: reuse this coin's last real quote if it's recent, else the flat fee."""
    hit = _recent_cost.get((mint, side))
    if hit and time.time() - hit[0] < 1800:
        return hit[1], f"recent quote ({hit[1]:.2f}% cost)"
    return fee_pct, f"estimated ({fee_pct:g}% fee)"


def simulate_buy(mint, usd, price, fee_pct):
    """Coins received for `usd`, and how the fill was worked out."""
    if USE_JUPITER_QUOTES and mint in DECIMALS and usd > NETWORK_FEE_USD:
        try:
            out = jupiter_quote(USDC_MINT, mint, int((usd - NETWORK_FEE_USD) * 1e6))
            coins = out / 10 ** DECIMALS[mint]
            _recent_cost[(mint, "buy")] = (time.time(), (1 - coins * price / usd) * 100)
            return coins, "jupiter quote"
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return None, "no route"  # Jupiter can't trade this coin: no real buy possible
        except Exception:
            pass
    cost, how = fallback_cost(mint, "buy", fee_pct)
    return usd * (1 - cost / 100) / price, how


def simulate_sell(mint, coins, price, fee_pct):
    """Dollars received for selling `coins`, and how the fill was worked out.
    Returns (None, "no route") when Jupiter can't find any way to sell the coin."""
    if USE_JUPITER_QUOTES and mint in DECIMALS:
        try:
            out = jupiter_quote(mint, USDC_MINT, int(coins * 10 ** DECIMALS[mint]))
            usd = max(out / 1e6 - NETWORK_FEE_USD, 0)
            if coins * price > 0:
                _recent_cost[(mint, "sell")] = (time.time(), (1 - usd / (coins * price)) * 100)
            return usd, "jupiter quote"
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return None, "no route"
        except Exception:
            pass
    cost, how = fallback_cost(mint, "sell", fee_pct)
    return coins * price * (1 - cost / 100), how


# ---------------------------------------------------------------------------
# New-launch scanner + safety checks
# ---------------------------------------------------------------------------

def fetch_new_candidates():
    """Recently promoted Solana coins from DexScreener's latest profiles and boosts."""
    mints = []
    for url in ("https://api.dexscreener.com/token-profiles/latest/v1",
                "https://api.dexscreener.com/token-boosts/latest/v1"):
        try:
            for item in get_json(url):
                if item.get("chainId") == "solana" and item.get("tokenAddress"):
                    mints.append(item["tokenAddress"])
        except Exception:
            pass
    return list(dict.fromkeys(mints))  # de-duplicate, keep order


def helius_key():
    return os.environ.get(HELIUS_KEY_ENV, "").strip() or None


def helius_rpc(key, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(f"https://mainnet.helius-rpc.com/?api-key={key}", data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        out = json.load(resp)
    if "error" in out:
        raise RuntimeError(out["error"].get("message", out["error"]))
    return out["result"]


_last_graduation_sig = None


def fetch_graduations():
    """Helius: pump.fun coins that just graduated to a real pool, newest first."""
    global _last_graduation_sig
    key = helius_key()
    if not key:
        return []
    opts = {"limit": HELIUS_MAX_TX_PER_SCAN}
    if _last_graduation_sig:
        opts["until"] = _last_graduation_sig  # only transactions since last time
    sigs = helius_rpc(key, "getSignaturesForAddress", [PUMP_MIGRATION_ACCOUNT, opts])
    if sigs:
        _last_graduation_sig = sigs[0]["signature"]
    mints = []
    for s in sigs:
        if s.get("err"):
            continue
        tx = helius_rpc(key, "getTransaction", [s["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        for bal in ((tx or {}).get("meta") or {}).get("postTokenBalances") or []:
            mint = bal.get("mint", "")
            if mint.endswith("pump") and mint not in mints:
                mints.append(mint)
    return mints


SYSTEM_PROGRAM = "11111111111111111111111111111111"


def safety_check(mint, m):
    """Check a coin straight from the blockchain (via Helius) plus DexScreener's
    market data. Returns (passed, liquidity_usd, list of problems).

    Cheap checks run first; if they already fail, no Helius requests are spent."""
    problems = []
    liquidity = m.get("liquidity") or 0

    # Only pump.fun coins: their liquidity is locked by design (a bonding curve,
    # then a pool whose LP tokens are burned), so the pool can't be pulled.
    if not mint.endswith("pump"):
        problems.append("not a pump.fun coin (can't verify its liquidity is locked)")
    if m.get("dex") == "pumpfun":
        problems.append("still on the bonding curve (hasn't graduated to a real pool)")
    if liquidity < SAFE_MIN_LIQUIDITY_USD:
        problems.append(f"liquidity only ${liquidity:,.0f}")
    if problems:
        return False, liquidity, problems

    key = helius_key()
    if not key:
        raise RuntimeError(f"no Helius key ({HELIUS_KEY_ENV} not set)")

    # The coin itself: can the creator still mint more, freeze wallets, or tax transfers?
    acc = helius_rpc(key, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
    info = acc["data"]["parsed"]["info"]
    if info.get("mintAuthority"):
        problems.append("creator can mint more coins")
    if info.get("freezeAuthority"):
        problems.append("creator can freeze your coins (honeypot risk)")
    for ext in info.get("extensions") or []:
        if ext.get("extension") == "transferFeeConfig":
            bps = ((ext.get("state") or {}).get("newerTransferFee") or {}).get(
                "transferFeeBasisPoints", 0)
            if bps:
                problems.append(f"has a {bps / 100:g}% transfer tax")
    supply = int(info["supply"]) or 1

    # Who holds it? Top 20 token accounts -> their owners -> keep only real
    # wallets (owned by the System Program). Pools and program vaults are skipped,
    # since they aren't people who can dump on you.
    largest = helius_rpc(key, "getTokenLargestAccounts", [mint])["value"]
    accounts = helius_rpc(key, "getMultipleAccounts",
                          [[a["address"] for a in largest], {"encoding": "jsonParsed"}])["value"]
    owner_of = {}
    for a, data in zip(largest, accounts):
        try:
            owner_of[a["address"]] = data["data"]["parsed"]["info"]["owner"]
        except (TypeError, KeyError):
            pass
    owners = list(dict.fromkeys(owner_of.values()))
    owner_accounts = helius_rpc(key, "getMultipleAccounts",
                                [owners, {"encoding": "base64"}])["value"] if owners else []
    wallets = {o for o, data in zip(owners, owner_accounts)
               if data and data.get("owner") == SYSTEM_PROGRAM}

    held_by = {}
    for a in largest:
        owner = owner_of.get(a["address"])
        if owner in wallets:
            held_by[owner] = held_by.get(owner, 0) + int(a["amount"]) / supply * 100
    shares = sorted(held_by.values(), reverse=True)
    top10 = sum(shares[:10])
    if top10 > SAFE_MAX_TOP10_PCT:
        problems.append(f"top 10 wallets own {top10:.0f}%")
    if shares and shares[0] > SAFE_MAX_ONE_HOLDER_PCT:
        problems.append(f"one wallet owns {shares[0]:.0f}%")

    return not problems, liquidity, problems


def log_scan(mint, symbol, passed, problems, source):
    append_row(SCAN_FILE, ["time", "coin", "address", "passed", "problems", "source"],
               [now(), symbol, mint, passed, "; ".join(problems), source])


_pending_graduates = {}  # mint -> when Helius first saw it graduate


def mark_seen(state, mint, reason):
    """Remember a coin was already dealt with, so the scanner never checks it twice."""
    seen = state["seen"]
    seen[mint] = reason
    while len(seen) > 5000:  # keep the file small; forget the oldest
        del seen[next(iter(seen))]


def scan_new_coins(state):
    """Find new launches, safety-check them, and add them to the watch list."""
    watch = state["new_coins"]

    # Forget old coins nobody holds.
    held = {m for w in state["wallets"].values() for m in w["positions"]}
    for mint in list(watch):
        too_old = time.time() - watch[mint]["added"] > NEW_WATCH_HOURS * 3600
        if too_old and mint not in held:
            del watch[mint]
            mark_seen(state, mint, "stopped watching (too old)")

    # Fresh graduations from Helius go to the front of the queue.
    try:
        for mint in fetch_graduations():
            _pending_graduates.setdefault(mint, time.time())
    except Exception as e:
        print(f"[{now()}] Helius check failed ({e}); using DexScreener only this round.")
    for mint, first_seen in list(_pending_graduates.items()):
        if mint in watch or mint in state["seen"] or (
                time.time() - first_seen > GRADUATE_RETRY_MINUTES * 60):
            del _pending_graduates[mint]

    try:
        dex = fetch_new_candidates()
        candidates = [m for m in list(_pending_graduates) + dex
                      if m not in watch and m not in state["seen"]
                      and m not in TOKENS.values()]
        candidates = list(dict.fromkeys(candidates))
        market = fetch_market(candidates) if candidates else {}
    except Exception as e:
        print(f"[{now()}] Scan failed ({e}); trying again next round.")
        return

    checks = 0
    for mint in candidates:
        if checks >= NEW_MAX_CHECKS_PER_SCAN:
            break
        source = "helius graduation" if mint in _pending_graduates else "dexscreener"
        m = market.get(mint)
        if mint in _pending_graduates and (
                not m or m["dex"] == "pumpfun" or not m["liquidity"]):
            continue  # just graduated; DexScreener hasn't caught up yet - try next scan
        if not m or m["age_hours"] > NEW_MAX_AGE_HOURS:
            mark_seen(state, mint, "not new")
            continue
        checks += 1
        try:
            passed, liquidity, problems = safety_check(mint, m)
        except Exception as e:
            print(f"  scan: couldn't check {m['symbol']} ({e})")
            continue
        time.sleep(1)  # pace requests to Helius's free plan

        log_scan(mint, m["symbol"], passed, problems, source)
        _pending_graduates.pop(mint, None)
        verdict = "PASS" if passed else "FAIL: " + "; ".join(problems)
        tag = " [graduated]" if source == "helius graduation" else ""
        print(f"  scan: {m['symbol']:<10} {m['age_hours']:4.1f}h old{tag}  {verdict}")
        if len(watch) >= NEW_MAX_WATCHED:
            # Make room by dropping the oldest coin nobody holds.
            unheld = [x for x in watch if x not in held]
            if not unheld:
                break
            oldest = min(unheld, key=lambda x: watch[x]["added"])
            del watch[oldest]
            mark_seen(state, oldest, "stopped watching (list full)")
        watch[mint] = {"symbol": m["symbol"], "added": time.time(),
                       "safe": passed, "liquidity": liquidity}


# ---------------------------------------------------------------------------
# Strategies
#
# A strategy is a buy rule, an exit style, and which coins it trades:
#   universe "core" - the established coins in TOKENS
#   universe "new"  - newly launched coins found by the scanner
#   universe "both" - both, using "buy" for established coins and "buy_new" for new ones
# Exit styles:
#   "fixed"   - sell everything at -STOP_LOSS_PCT or +TAKE_PROFIT_PCT
#   "trail"   - no profit cap; sell everything once price falls the trail % from its peak
#   "moonbag" - at MOONBAG_AT_X sell MOONBAG_SELL_PCT (gets your money back),
#               trail the rest like "trail"
#   "none"    - never sell (benchmark)
# ---------------------------------------------------------------------------

def momentum(mint, m, history):
    """Chase strength: price jumped in the last 5 min and the hour is positive."""
    return m["m5"] > 2 and m["h1"] > 0


def dip_buyer(mint, m, history):
    """Buy the dip: down a lot over the hour, but starting to bounce."""
    return m["h1"] < -5 and m["m5"] > 0


def ma_crossover(mint, m, history):
    """Short average of recent prices crosses above the long average."""
    prices = history.get(mint, [])
    if len(prices) < 21:
        return False  # not enough data yet (needs ~10 min of SLOW_SECONDS samples)
    short_now, long_now = avg(prices[-5:]), avg(prices[-20:])
    short_prev, long_prev = avg(prices[-6:-1]), avg(prices[-21:-1])
    return short_prev <= long_prev and short_now > long_now


def ma_exit(mint, m, history):
    """Moving-average strategy also sells when the short average drops below the long one."""
    prices = history.get(mint, [])
    return len(prices) >= 20 and avg(prices[-5:]) < avg(prices[-20:])


def ma_trend(mint, m, history):
    """Stricter crossover: the short average must clear the long one by a real margin
    (bigger than the fees) and the long average must already be rising. Skips the
    tiny wiggles that made ma_crossover pay fees for nothing."""
    prices = history.get(mint, [])
    if len(prices) < 30:
        return False
    short_now, long_now = avg(prices[-5:]), avg(prices[-20:])
    long_before = avg(prices[-30:-10])
    gap_pct = (short_now / long_now - 1) * 100
    return gap_pct >= MA_MIN_GAP_PCT and long_now > long_before


def ma_trend_exit(mint, m, history):
    """Sell once the short average falls clearly below the long one."""
    prices = history.get(mint, [])
    if len(prices) < 20:
        return False
    return (avg(prices[-5:]) / avg(prices[-20:]) - 1) * 100 <= -MA_MIN_GAP_PCT


def hodl(mint, m, history):
    """Benchmark: buy once and never sell. A strategy should beat this to be worth it."""
    return True


def new_pullback(mint, m, history):
    """Buy the dip after a run-up instead of the spike: the coin rose at least
    PULLBACK_RUNUP_PCT within the last hour, has since dropped PULLBACK_MIN..MAX_PCT
    from that high, and buyers still outnumber sellers."""
    prices = history.get(mint, [])[-120:]  # last hour at one sample per 30s
    if len(prices) < 10:
        return False
    high = max(prices)
    low_before_high = min(prices[:prices.index(high) + 1])
    runup = (high / low_before_high - 1) * 100
    dip = (1 - m["price"] / high) * 100
    return (runup >= PULLBACK_RUNUP_PCT
            and PULLBACK_MIN_PCT <= dip <= PULLBACK_MAX_PCT
            and m["buys5m"] > m["sells5m"])


def new_momentum(mint, m, history):
    """New coin is rising and more people are buying than selling in the last 5 min."""
    return m["m5"] > 5 and m["buys5m"] > m["sells5m"] * 1.2


STRATEGIES = {
    # Established coins
    "momentum": {"universe": "core", "buy": momentum, "exit": "fixed"},
    "dip_buyer": {"universe": "core", "buy": dip_buyer, "exit": "fixed"},
    "ma_crossover": {"universe": "core", "buy": ma_crossover, "exit": "fixed",
                     "signal_exit": ma_exit},
    "momentum_trail": {"universe": "core", "buy": momentum, "exit": "trail"},
    "momentum_moonbag": {"universe": "core", "buy": momentum, "exit": "moonbag"},
    # hodl splits its wallet evenly across all established coins.
    "hodl": {"universe": "core", "buy": hodl, "exit": "none",
             "core_pct": 100 / len(TOKENS)},
    # Newly launched coins
    "new_safe_trail": {"universe": "new", "safe_only": True, "buy": new_momentum,
                       "exit": "trail"},
    "new_safe_moonbag": {"universe": "new", "safe_only": True, "buy": new_momentum,
                         "exit": "moonbag"},
    # Control group: same rules but ignores the safety checks. Compare it with
    # new_safe_trail to see whether the checks actually protect you.
    "new_unsafe_trail": {"universe": "new", "safe_only": False, "buy": new_momentum,
                         "exit": "trail"},
    # Both kinds in one wallet: most money in established coins, a capped
    # slice in new coins that passed the safety checks.
    "combo": {"universe": "both", "safe_only": True, "buy": momentum,
              "buy_new": new_momentum, "exit": "moonbag",
              "core_pct": COMBO_CORE_POSITION_PCT},

    # Improved versions, added after the first test run. Each runs next to its
    # original so you can see whether the change actually helps.
    # 1) Crossover that ignores moves too small to beat the fees.
    "ma_trend": {"universe": "core", "buy": ma_trend, "exit": "fixed",
                 "signal_exit": ma_trend_exit},
    # 2) "no_rebuy": never buy the same new coin twice. The first run kept
    #    re-buying coins that were sliding (one coin 14 times), losing each time.
    # 3) Buy the dip after a run-up instead of the spike itself.
    "new_pullback": {"universe": "new", "safe_only": True, "buy": new_pullback,
                     "exit": "trail", "no_rebuy": True},
    "new_safe_once": {"universe": "new", "safe_only": True, "buy": new_momentum,
                      "exit": "trail", "no_rebuy": True},
    "new_unsafe_once": {"universe": "new", "safe_only": False, "buy": new_momentum,
                        "exit": "trail", "no_rebuy": True},
    "combo_once": {"universe": "both", "safe_only": True, "buy": momentum,
                   "buy_new": new_momentum, "exit": "moonbag",
                   "core_pct": COMBO_CORE_POSITION_PCT, "no_rebuy": True},
}


def avg(xs):
    return sum(xs) / len(xs)


# ---------------------------------------------------------------------------
# Pretend wallets
# ---------------------------------------------------------------------------

def empty_wallet():
    return {"cash": STARTING_CASH, "positions": {}, "trades": 0, "wins": 0}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {"started": now(), "wallets": {}}
    for name in STRATEGIES:  # wallets for any strategy added since the last run
        state["wallets"].setdefault(name, empty_wallet())
    state.setdefault("new_coins", {})
    state.setdefault("seen", state.pop("rejected", {}))
    return state


def save_state(state):
    # Write to a temp file and swap it in, so a crash mid-save can't corrupt the
    # wallets. If OneDrive or antivirus has the file locked, try again next time.
    state.pop("dirty", None)
    state["last_saved"] = time.time()
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"[{now()}] Couldn't save state.json ({e}); will retry.")


def append_row(path, header, row):
    """Append a CSV row, retrying briefly if the file is locked (e.g. by OneDrive or Excel)."""
    for attempt in range(5):
        try:
            is_new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(header)
                w.writerow(row)
            return
        except OSError as e:
            if attempt == 4:
                print(f"[{now()}] Couldn't write {os.path.basename(path)} ({e}); row skipped.")
            else:
                time.sleep(0.5)


def is_new_coin(mint):
    return mint not in TOKENS.values()


def symbol_of(state, mint):
    for sym, addr in TOKENS.items():
        if addr == mint:
            return sym
    return state["new_coins"].get(mint, {}).get("symbol") or mint[:6]


TRADE_HEADER = ["time", "strategy", "side", "coin", "address", "price", "usd", "pnl_pct",
                "reason", "cost_pct", "fill"]


def log_trade(state, strategy, side, mint, price, usd, pnl_pct="", reason="",
              cost_pct=0.0, fill=""):
    """cost_pct: % lost to fees + price impact on this trade, versus the market price."""
    sym = symbol_of(state, mint)
    append_row(TRADES_FILE, TRADE_HEADER,
               [now(), strategy, side, sym, mint, f"{price:.10g}", f"{usd:.2f}", pnl_pct,
                reason, f"{cost_pct:.2f}", fill])
    state["dirty"] = True
    pnl = f" ({pnl_pct:+.1f}%)" if pnl_pct != "" else ""
    why = f" [{reason}]" if reason else ""
    print(f"  {strategy:<17} {side:<4} {sym:<10} ${usd:,.2f} @ {price:.10g}{pnl}{why}"
          f"  cost {cost_pct:.1f}%")


def buy(state, name, mint, price):
    wallet = state["wallets"][name]
    new = is_new_coin(mint)
    size_pct = NEW_POSITION_PCT if new else STRATEGIES[name].get("core_pct", POSITION_PCT)
    fee = NEW_FEE_PCT if new else FEE_PCT
    usd = min(STARTING_CASH * size_pct / 100, wallet["cash"])
    if usd < 1:
        return
    coins, fill = simulate_buy(mint, usd, price, fee)
    if coins is None:
        return  # no route: this coin couldn't really be bought right now
    cost_pct = (1 - coins * price / usd) * 100
    wallet["cash"] -= usd
    wallet["positions"][mint] = {"coins": coins, "cost": usd, "entry": price, "peak": price}
    log_trade(state, name, "BUY", mint, price, usd, cost_pct=cost_pct, fill=fill)


# Set at startup after downtime: sells before this time are labelled "gap sell".
gap_sells_until = 0.0


def sell(state, name, mint, price, reason, fraction=1.0):
    """Sell `fraction` of a position (1.0 = all of it). Returns False if it couldn't sell."""
    wallet = state["wallets"][name]
    pos = wallet["positions"][mint]
    fee = NEW_FEE_PCT if is_new_coin(mint) else FEE_PCT
    coins = pos["coins"] * fraction
    cost = pos["cost"] * fraction
    usd, fill = simulate_sell(mint, coins, price, fee)
    if usd is None:
        # No way to sell right now. Often the pool was pulled (a rug), sometimes the
        # coin is just moving to a new pool. Retry each minute; after a while, write
        # it off at $0, the way a real holder would be stuck with it.
        first = pos.setdefault("no_route_since", time.time())
        if time.time() - first < NO_ROUTE_WRITE_OFF_SECONDS:
            pos["retry_after"] = time.time() + 60
            return False
        coins = pos["coins"]
        cost = pos["cost"]
        fraction = 1.0
        usd, fill = 0.0, "no route - counted as $0"
        reason = f"written off: no way to sell for {NO_ROUTE_WRITE_OFF_SECONDS // 60}+ min (likely rugged)"
    pos.pop("no_route_since", None)
    cost_pct = (1 - usd / (coins * price)) * 100 if coins * price > 0 else 0
    if time.time() < gap_sells_until:
        reason = "gap sell: " + reason

    wallet["cash"] += usd
    wallet["trades"] += 1
    pnl_pct = (usd / cost - 1) * 100
    if pnl_pct > 0:
        wallet["wins"] += 1

    if fraction >= 1.0:
        del wallet["positions"][mint]
    else:
        pos["coins"] -= coins
        pos["cost"] -= cost
    log_trade(state, name, "SELL", mint, price, usd, round(pnl_pct, 2), reason,
              cost_pct=cost_pct, fill=fill)


def wallet_value(wallet, prices):
    value = wallet["cash"]
    for mint, pos in wallet["positions"].items():
        value += pos["coins"] * prices.get(mint, pos["entry"])
    return value


def snapshot_equity(state, prices):
    """Record every wallet's current value, for charting how it changed over time."""
    for name, w in state["wallets"].items():
        append_row(EQUITY_FILE, ["time", "strategy", "value", "cash", "positions"],
                   [now(), name, f"{wallet_value(w, prices):.2f}", f"{w['cash']:.2f}",
                    len(w["positions"])])


# ---------------------------------------------------------------------------
# Trading logic
# ---------------------------------------------------------------------------

def check_exits(state, prices):
    """Fast loop: apply each strategy's exit rule to its open positions."""
    for name, strat in STRATEGIES.items():
        wallet = state["wallets"][name]
        for mint in list(wallet["positions"]):
            if mint not in prices:
                continue
            pos = wallet["positions"][mint]
            if time.time() < pos.get("retry_after", 0):
                continue  # couldn't sell a moment ago; wait before asking again
            price = prices[mint]
            pos["peak"] = max(pos.get("peak", pos["entry"]), price)
            change = (price / pos["entry"] - 1) * 100
            from_peak = (price / pos["peak"] - 1) * 100
            trail = NEW_TRAIL_PCT if is_new_coin(mint) else TRAIL_PCT
            style = strat["exit"]

            if style == "fixed":
                if change <= -STOP_LOSS_PCT:
                    sell(state, name, mint, price, "stop-loss")
                elif change >= TAKE_PROFIT_PCT:
                    sell(state, name, mint, price, "take-profit")

            elif style in ("trail", "moonbag"):
                if (style == "moonbag" and not pos.get("half_sold")
                        and price >= pos["entry"] * MOONBAG_AT_X):
                    if sell(state, name, mint, price, f"{MOONBAG_AT_X:g}x - took money out",
                            fraction=MOONBAG_SELL_PCT / 100) is not False:
                        pos["half_sold"] = True
                elif from_peak <= -trail:
                    sell(state, name, mint, price,
                         f"trailing stop ({from_peak:.0f}% from peak)")


def look_for_buys(state, market, history, prices):
    """Slow loop: strategy-specific exit signals, then new buys.

    Signals come from DexScreener's market data, but trades are recorded at the
    live price in `prices` (Jupiter, refreshed every few seconds): DexScreener's
    price can be 30+ seconds old, which for a fast new coin is a different price."""
    core = set(TOKENS.values())
    for name, strat in STRATEGIES.items():
        wallet = state["wallets"][name]

        signal_exit = strat.get("signal_exit")
        if signal_exit:
            for mint in list(wallet["positions"]):
                if mint in market and signal_exit(mint, market[mint], history):
                    sell(state, name, mint, prices.get(mint, market[mint]["price"]), "signal")

        for mint, m in market.items():
            if mint in wallet["positions"]:
                continue  # one position per coin at a time
            if mint in core:
                if strat["universe"] == "new" or m["liquidity"] < MIN_LIQUIDITY_USD:
                    continue
                rule = strat["buy"]
            else:
                info = state["new_coins"].get(mint)
                if strat["universe"] == "core" or not info:
                    continue
                if strat["safe_only"] and not info["safe"]:
                    continue
                if strat.get("no_rebuy") and wallet.get("bought_once", {}).get(mint):
                    continue
                if strat["universe"] == "both":
                    in_new = sum(p["cost"] for x, p in wallet["positions"].items()
                                 if x not in core)
                    if in_new + STARTING_CASH * NEW_POSITION_PCT / 100 > (
                            STARTING_CASH * COMBO_NEW_MAX_PCT / 100):
                        continue  # new-coin slice is full
                rule = strat.get("buy_new", strat["buy"])
            if name == "hodl" and wallet.get("bought_once", {}).get(mint):
                continue
            if rule(mint, m, history):
                buy(state, name, mint, prices.get(mint, m["price"]))
                if mint in wallet["positions"] and (name == "hodl" or strat.get("no_rebuy")):
                    wallet.setdefault("bought_once", {})[mint] = True


def print_summary(state, prices):
    print(f"\n{'strategy':<17} {'value':>10} {'return':>8} {'trades':>7} {'win %':>6}  holding")
    rows = sorted(
        state["wallets"].items(), key=lambda kv: wallet_value(kv[1], prices), reverse=True
    )
    for name, w in rows:
        value = wallet_value(w, prices)
        ret = (value / STARTING_CASH - 1) * 100
        win = f"{w['wins'] / w['trades'] * 100:.0f}" if w["trades"] else "-"
        held = ", ".join(symbol_of(state, m) for m in w["positions"]) or "-"
        print(f"{name:<17} ${value:>9,.2f} {ret:>+7.2f}% {w['trades']:>7} {win:>6}  {held}")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main():
    # Coin names can contain emoji or other scripts; never crash printing them.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    if "--reset" in sys.argv:
        for path in (STATE_FILE, TRADES_FILE, SCAN_FILE, EQUITY_FILE):
            if os.path.exists(path):
                os.remove(path)
        print("Reset: all pretend wallets back to $%.0f." % STARTING_CASH)

    state = load_state()
    history = {}
    prices = {}
    from_jupiter = set()
    next_slow = next_scan = next_equity = 0
    print(f"Paper trading since {state['started']}. Core coins: {', '.join(TOKENS)}")
    print("Helius graduation feed: " + ("on" if helius_key() else
          f"off ({HELIUS_KEY_ENV} not set)"))
    print(f"Exit checks every {FAST_SECONDS}s, buys every {SLOW_SECONDS}s, "
          f"new-coin scan every {SCAN_SECONDS}s. Press Ctrl+C to stop.\n")

    # Was the bot off for a while? Record it, and label the catch-up sells.
    global gap_sells_until
    offline = time.time() - state.get("last_saved", time.time())
    if offline > RESTART_GAP_SECONDS:
        note = f"bot was offline {offline / 60:.0f} min"
        print(f"[{now()}] Restarting after downtime: {note}. "
              f"Sells in the next {GAP_SELL_SECONDS}s are labelled 'gap sell'.\n")
        append_row(TRADES_FILE, TRADE_HEADER,
                   [now(), "-", "RESTART", "-", "-", "", "", "", note, "", ""])
        gap_sells_until = time.time() + GAP_SELL_SECONDS

    try:
        while True:
          # Any unexpected error is printed and the bot carries on with the next
          # round, so one weird coin can't stop an overnight run.
          try:
            watched = list(TOKENS.values()) + list(state["new_coins"])
            held = {m for w in state["wallets"].values() for m in w["positions"]}
            watched += [m for m in held if m not in watched]

            # Fast loop: fresh prices, then stops.
            try:
                fast = fetch_fast_prices(watched)
                prices.update(fast)
                from_jupiter = set(fast)
            except Exception as e:
                print(f"[{now()}] Jupiter price check failed ({e}); using last known prices.")
                from_jupiter = set()
            check_exits(state, prices)

            if prices and time.time() >= next_equity:
                next_equity = time.time() + EQUITY_SNAPSHOT_SECONDS
                snapshot_equity(state, prices)

            # Scanner: find and safety-check new launches.
            if time.time() >= next_scan:
                next_scan = time.time() + SCAN_SECONDS
                scan_new_coins(state)

            # Slow loop: market data, then buys.
            if time.time() >= next_slow:
                next_slow = time.time() + SLOW_SECONDS
                try:
                    market = fetch_market(watched)
                except Exception as e:
                    print(f"[{now()}] DexScreener failed ({e}); skipping buys this round.")
                    market = {}
                for mint, m in market.items():
                    if mint not in from_jupiter:  # coins Jupiter doesn't price yet
                        prices[mint] = m["price"]
                    history.setdefault(mint, []).append(m["price"])
                    history[mint] = history[mint][-120:]
                if market:
                    core = "  ".join(
                        f"{s} {prices.get(a, 0):.6g}" for s, a in TOKENS.items() if a in market
                    )
                    n_safe = sum(1 for c in state["new_coins"].values() if c["safe"])
                    print(f"[{now()}] {core}  | new coins watched: "
                          f"{len(state['new_coins'])} ({n_safe} passed safety)")
                    look_for_buys(state, market, history, prices)

                save_state(state)  # every SLOW_SECONDS...
            if state.pop("dirty", False):
                save_state(state)  # ...and right after any trade
          except Exception:
            print(f"[{now()}] Unexpected error - carrying on:\n{traceback.format_exc()}")
            save_state(state)
          time.sleep(FAST_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if prices:
            print_summary(state, prices)
        save_state(state)


if __name__ == "__main__":
    main()
