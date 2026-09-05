#!/usr/bin/env python3
"""Stage 2: parsed transactions via Helius Enhanced API, batched 100/call.

    python fetch_transactions.py --limit 500          # fetch + load into DuckDB
    python fetch_transactions.py --sample 5           # print 5 parsed txs, no DB write

Reads signatures from `vault_signatures`, POSTs them to /v0/transactions at
2 req/s, caches every batch as JSON, and writes derived legs to `swap_legs`.
"""
import argparse
import json
import time

import config
import store
import swap_parse
from concurrent.futures import ThreadPoolExecutor

from http_util import _retryable_post, cache_key, cached_json_gz, enhanced_limiter

BATCH = 100
# The 2 req/s cap is enforced by the shared limiter; requests take ~2s each, so
# they must overlap or the effective rate collapses to 1/latency.
WORKERS = 4


def load_signatures(limit=None, sample=None, seed=42):
    con = store.connect(read_only=True)
    if sample:
        q = f"SELECT signature FROM vault_signatures WHERE err IS NULL USING SAMPLE {sample} ROWS (reservoir, {seed})"
    else:
        q = "SELECT signature FROM vault_signatures WHERE err IS NULL ORDER BY block_time DESC"
        if limit:
            q += f" LIMIT {limit}"
    sigs = [r[0] for r in con.execute(q).fetchall()]
    con.close()
    return sigs


def batch_path(sigs):
    return config.ENHANCED_CACHE / f"{cache_key(*sigs)}.json.gz"


def fetch_batch(sigs, use_cache=True):
    def produce():
        return _retryable_post(config.ENHANCED_TX_URL, {"transactions": sigs}, enhanced_limiter)

    if not use_cache:
        return produce()
    return cached_json_gz(batch_path(sigs), produce)


def fetch_batch_safe(sigs, use_cache=True):
    """A batch that will not die is worth more than one that fails fast: a single
    exhausted retry chain must not throw away a 30-minute run. Returns [] and
    lets the caller re-run -- cached batches make the retry pass nearly free."""
    try:
        return fetch_batch(sigs, use_cache)
    except Exception as e:
        print(f"    !! batch failed, skipping: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None


def iter_batches(sigs, use_cache=True):
    """Yield (batch_no, total_batches, txs) so callers never hold the whole corpus."""
    chunks = [sigs[i:i + BATCH] for i in range(0, len(sigs), BATCH)]
    total_batches = len(chunks)
    t0, cached_hits, seen, done = time.time(), 0, 0, 0
    failed = [0]

    # Waves keep at most WORKERS*4 batches of parsed JSON alive at once.
    wave = WORKERS * 4
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for w in range(0, total_batches, wave):
            group = chunks[w:w + wave]
            cached_hits += sum(batch_path(c).exists() for c in group)
            for txs in pool.map(lambda c: fetch_batch_safe(c, use_cache), group):
                if txs is None:
                    failed[0] += 1
                    continue
                seen += len(txs)
                done += 1
                if done % 25 == 0 or done == total_batches:
                    el = time.time() - t0
                    eta = (total_batches - done) * (el / done) / 60
                    print(f"  batch {done}/{total_batches}  {seen}/{len(sigs)} txs  "
                          f"({cached_hits} cached, {seen/max(el,1e-6):.0f} tx/s, "
                          f"ETA {eta:.1f} min, {failed[0]} failed)", flush=True)
                yield done, total_batches, txs


def fetch_all(sigs, use_cache=True):
    out = []
    for _, _, txs in iter_batches(sigs, use_cache):
        out += txs
    return out


def to_legs(txs):
    legs = []
    for tx in txs:
        if tx.get("transactionError"):
            continue
        legs.extend(swap_parse.swap_legs(tx))
    return legs


def open_legs_table(con, reset=False):
    if reset:
        con.execute("DROP TABLE IF EXISTS swap_legs")
    con.execute("""
        CREATE TABLE IF NOT EXISTS swap_legs (
            signature VARCHAR, block_time BIGINT, source VARCHAR, account VARCHAR,
            token_mint VARCHAR, token_delta DOUBLE, cash_mint VARCHAR,
            cash_symbol VARCHAR, cash_delta DOUBLE, side VARCHAR,
            cash_abs DOUBLE, token_abs DOUBLE
        );
    """)
    return con


COLS = ["signature", "block_time", "source", "account", "token_mint", "token_delta",
        "cash_mint", "cash_symbol", "cash_delta", "side", "cash_abs", "token_abs"]


def insert_legs(con, legs):
    if not legs:
        return
    con.executemany(
        f"INSERT INTO swap_legs VALUES ({','.join('?' * len(COLS))})",
        [[l[c] for c in COLS] for l in legs],
    )


def print_sample(txs, n=5):
    shown = 0
    for tx in txs:
        legs = list(swap_parse.swap_legs(tx))
        if not legs:
            continue
        shown += 1
        print("=" * 78)
        print(f"{tx['signature']}")
        print(f"  type={tx.get('type')}  source={tx.get('source')}  "
              f"time={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(tx['timestamp']))}Z")
        print(f"  feePayer={tx.get('feePayer')}   events={tx.get('events') or '{} (empty)'}")
        print(f"  legs derived from balance deltas: {len(legs)}")
        for l in legs:
            usd = f"${l['cash_abs']:,.2f}" if l["cash_symbol"] in ("USDC", "USDT") else f"{l['cash_abs']:.6f} SOL (needs SOL/USD)"
            print(f"    {l['side']:4}  {l['account']}")
            print(f"          token {l['token_delta']:+,.6f}  {l['token_mint']}")
            print(f"          cash  {l['cash_delta']:+,.6f}  {l['cash_symbol']}   -> {usd}")
        if shown >= n:
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="most recent N signatures")
    ap.add_argument("--sample", type=int, help="random N signatures (implies dry run)")
    ap.add_argument("--print-sample", type=int, default=0, help="pretty-print N parsed txs")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="do not write to DuckDB")
    ap.add_argument("--append", action="store_true", help="keep existing swap_legs rows")
    args = ap.parse_args()

    sigs = load_signatures(limit=args.limit, sample=args.sample)
    print(f"Signatures to fetch: {len(sigs)}  ({(len(sigs)+BATCH-1)//BATCH} batches @ 2 req/s "
          f"~= {(len(sigs)+BATCH-1)//BATCH/2/60:.1f} min uncached)")

    dry = args.dry_run or args.sample
    con = None if dry else open_legs_table(store.connect(), reset=not args.append)

    n_txs = n_legs = n_err = 0
    printed = False
    for _, _, txs in iter_batches(sigs, use_cache=not args.no_cache):
        n_txs += len(txs)
        n_err += sum(1 for t in txs if t.get("transactionError"))
        legs = to_legs(txs)
        n_legs += len(legs)
        if con is not None:
            insert_legs(con, legs)
        if args.print_sample and not printed:
            print_sample(txs, args.print_sample)
            printed = True

    print(f"\nParsed {n_txs} txs ({n_err} with on-chain errors) -> {n_legs} swap legs")
    missing = len(sigs) - n_txs
    if missing > 0:
        print(f"WARNING: {missing} signatures unaccounted for ({missing/len(sigs)*100:.2f}%). "
              f"Re-run to retry only the failed batches (cached ones are free).")
    if con is not None:
        total = con.execute("SELECT count(*) FROM swap_legs").fetchone()[0]
        con.close()
        print(f"`swap_legs` now holds {total} rows -> {config.DB_PATH}")


if __name__ == "__main__":
    main()
