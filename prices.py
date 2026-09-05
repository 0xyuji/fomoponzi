#!/usr/bin/env python3
"""Pricing layer (GeckoTerminal free API).

Two different jobs:
  * historical SOL/USD, hourly -- values the SOL leg of a swap at trade time
  * current token price by mint -- values leftover inventory for unrealized PnL

    python prices.py --sol          # load hourly SOL/USD into DuckDB
    python prices.py --tokens       # price every open position in cost_basis
"""
import argparse
import time

import config
import store
from http_util import cache_key, cached_json, gecko_limiter, _retryable_get

GT = "https://api.geckoterminal.com/api/v2"
# Deepest SOL/USDC pool on Solana; hourly candles reach ~41 days back in one call.
SOL_USDC_POOL = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"
PRICE_BATCH = 30
CACHE_TTL = 3600  # seconds; current prices go stale, history does not


def _fresh(path, ttl=CACHE_TTL):
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl


def fetch_sol_hourly():
    """[(hour_unix, close_usd)] for as far back as one OHLCV call reaches."""
    path = config.PRICE_CACHE / "sol_hourly.json"
    if not _fresh(path):
        path.unlink(missing_ok=True)

    def produce():
        url = f"{GT}/networks/solana/pools/{SOL_USDC_POOL}/ohlcv/hour"
        return _retryable_get(url, {"aggregate": 1, "limit": 1000, "currency": "usd"},
                              gecko_limiter)

    data = cached_json(path, produce)
    rows = data["data"]["attributes"]["ohlcv_list"]  # [ts, o, h, l, c, v]
    return [(int(r[0]), float(r[4])) for r in rows]


def load_sol_hourly(con):
    rows = fetch_sol_hourly()
    con.execute("CREATE OR REPLACE TABLE sol_hourly (hour_ts BIGINT PRIMARY KEY, price_usd DOUBLE)")
    con.executemany("INSERT INTO sol_hourly VALUES (?, ?)", rows)
    lo, hi, n = con.execute("SELECT min(hour_ts), max(hour_ts), count(*) FROM sol_hourly").fetchone()
    print(f"sol_hourly: {n} candles, "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(lo))} -> "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(hi))} UTC")
    return n


def fetch_token_prices(mints):
    """{mint: usd_price} -- GeckoTerminal takes up to 30 mints per call."""
    out = {}
    mints = sorted(set(mints))
    for i in range(0, len(mints), PRICE_BATCH):
        chunk = mints[i:i + PRICE_BATCH]
        path = config.PRICE_CACHE / f"tok_{cache_key(*chunk)}.json"
        if not _fresh(path):
            path.unlink(missing_ok=True)

        def produce():
            url = f"{GT}/simple/networks/solana/token_price/{','.join(chunk)}"
            return _retryable_get(url, {}, gecko_limiter)

        data = cached_json(path, produce)
        prices = ((data or {}).get("data", {}).get("attributes", {}) or {}).get("token_prices", {})
        for m in chunk:
            p = prices.get(m)
            # A missing mint means no live pool -- rugged or delisted. Price it at
            # zero rather than dropping it, or the loss silently disappears.
            out[m] = float(p) if p not in (None, "") else 0.0
        done = min(i + PRICE_BATCH, len(mints))
        print(f"  priced {done}/{len(mints)} mints "
              f"({sum(1 for m in out if out[m] == 0)} with no live pool)", flush=True)
    return out


def load_token_prices(con, mints):
    prices = fetch_token_prices(mints)
    con.execute("CREATE OR REPLACE TABLE token_prices (mint VARCHAR PRIMARY KEY, price_usd DOUBLE, fetched_at BIGINT)")
    now = int(time.time())
    con.executemany("INSERT INTO token_prices VALUES (?, ?, ?)",
                    [(m, p, now) for m, p in prices.items()])
    return len(prices)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sol", action="store_true", help="load hourly SOL/USD")
    ap.add_argument("--tokens", action="store_true", help="price open positions in cost_basis")
    args = ap.parse_args()
    con = store.connect()
    if args.sol or not args.tokens:
        load_sol_hourly(con)
    if args.tokens:
        mints = [r[0] for r in con.execute(
            "SELECT DISTINCT token_mint FROM cost_basis WHERE remaining_qty > 0").fetchall()]
        print(f"open positions across {len(mints)} distinct mints")
        n = load_token_prices(con, mints)
        print(f"token_prices: {n} rows")
    con.close()


if __name__ == "__main__":
    main()
