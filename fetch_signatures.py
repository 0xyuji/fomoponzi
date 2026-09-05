#!/usr/bin/env python3
"""Stage 1: every signature that touched the vault wallet in the lookback window.

    python fetch_signatures.py [--days 30] [--address <pubkey>] [--no-cache]

Pages getSignaturesForAddress (1000/call, 10 req/s), caches each page as JSON so
reruns are free, and writes the result to DuckDB table `vault_signatures`.
"""
import argparse
import time

import config
import store
from http_util import cache_key, cached_json, rpc_call

PAGE_LIMIT = 1000


def fetch_signatures(address: str, since_ts: int, use_cache: bool = True):
    """Walk backwards through the address's signature history until since_ts."""
    all_sigs = []
    before = None
    page = 0

    while True:
        page += 1
        params = [address, {"limit": PAGE_LIMIT}]
        if before:
            params[1]["before"] = before

        def produce():
            return rpc_call("getSignaturesForAddress", params)

        if use_cache:
            path = config.RPC_SIG_CACHE / f"{cache_key(address, before or 'head')}.json"
            batch = cached_json(path, produce)
        else:
            batch = produce()

        if not batch:
            print(f"  page {page}: empty, done")
            break

        stop = False
        kept = 0
        for entry in batch:
            bt = entry.get("blockTime")
            # blockTime can be null on very old/pruned blocks; keep it and let
            # the caller decide rather than silently truncating the walk.
            if bt is not None and bt < since_ts:
                stop = True
                break
            all_sigs.append(entry)
            kept += 1

        before = batch[-1]["signature"]
        oldest = batch[-1].get("blockTime")
        age = f"{(time.time() - oldest) / 86400:.1f}d old" if oldest else "unknown age"
        print(f"  page {page}: +{kept}/{len(batch)} kept  (total {len(all_sigs)}, oldest {age})")

        if stop:
            print(f"  reached {config.LOOKBACK_DAYS}-day cutoff, done")
            break
        if len(batch) < PAGE_LIMIT:
            print("  short page, end of history")
            break

    return all_sigs


def save(sigs):
    con = store.connect()
    store.init_schema(con)
    con.execute("CREATE OR REPLACE TEMP TABLE _incoming (signature VARCHAR, slot BIGINT, block_time BIGINT, err VARCHAR)")
    con.executemany(
        "INSERT INTO _incoming VALUES (?, ?, ?, ?)",
        [
            (s["signature"], s.get("slot"), s.get("blockTime"),
             None if s.get("err") is None else str(s["err"]))
            for s in sigs
        ],
    )
    con.execute("""
        INSERT INTO vault_signatures
        SELECT signature, any_value(slot), any_value(block_time), any_value(err)
        FROM _incoming
        WHERE signature NOT IN (SELECT signature FROM vault_signatures)
        GROUP BY signature
    """)
    total, ok, failed, oldest, newest = con.execute("""
        SELECT count(*), count(*) FILTER (err IS NULL), count(*) FILTER (err IS NOT NULL),
               min(block_time), max(block_time)
        FROM vault_signatures
    """).fetchone()
    path = store.export_csv(con, "vault_signatures")
    con.close()
    return total, ok, failed, oldest, newest, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=config.VAULT_ADDRESS)
    ap.add_argument("--days", type=int, default=config.LOOKBACK_DAYS)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    since_ts = int(time.time()) - args.days * 86400
    print(f"Vault:   {args.address}")
    print(f"Window:  last {args.days} days (since unix {since_ts})")
    print(f"Cache:   {'off' if args.no_cache else config.RPC_SIG_CACHE}")
    print("Fetching signatures...")

    t0 = time.time()
    sigs = fetch_signatures(args.address, since_ts, use_cache=not args.no_cache)
    total, ok, failed, oldest, newest, path = save(sigs)

    print()
    print(f"Fetched {len(sigs)} signatures in {time.time() - t0:.1f}s")
    print(f"In DuckDB `vault_signatures`: {total} rows ({ok} succeeded, {failed} failed on-chain)")
    if oldest:
        span = (newest - oldest) / 86400
        print(f"Time span: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(oldest))} UTC "
              f"-> {time.strftime('%Y-%m-%d %H:%M', time.gmtime(newest))} UTC ({span:.1f} days)")
    print(f"CSV: {path}")
    print(f"DB:  {config.DB_PATH}")


if __name__ == "__main__":
    main()
