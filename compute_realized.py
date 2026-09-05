#!/usr/bin/env python3
"""Realized-only PnL -- closed trades, no current prices needed.

Unrealized PnL depends on GeckoTerminal spot prices (slow, and $0 for rugged
mints). Realized PnL depends only on in-window buys and sells, so it can be
computed the moment `cost_basis` exists, and it is the more defensible number:
no mark-to-market assumption anywhere in it.

    python compute_realized.py [--snapshot]   # --snapshot copies the DB first,
                                              # so it can run while another
                                              # stage holds the write lock
"""
import argparse
import shutil

import duckdb

import config
import store


def build(con):
    con.execute("""
        CREATE OR REPLACE TABLE trader_realized AS
        WITH per_token AS (
          SELECT trader, token_mint,
                 total_bought, total_sold, matched_sold_qty, avg_cost,
                 -- proceeds attributable to the quantity whose cost we know
                 total_sell_usd * (matched_sold_qty / nullif(total_sold, 0)) AS matched_sell_usd,
                 total_sell_usd * (matched_sold_qty / nullif(total_sold, 0))
                   - matched_sold_qty * avg_cost                             AS realized_pnl
          FROM cost_basis
          WHERE basis_known AND total_sold > 0
        )
        SELECT trader,
               count(*)                        AS tokens_closed,
               round(sum(matched_sell_usd), 2) AS gross_proceeds_usd,
               round(sum(matched_sold_qty * avg_cost), 2) AS cost_of_sales_usd,
               round(sum(realized_pnl), 2)     AS realized_pnl
        FROM per_token GROUP BY 1
    """)
    n, = con.execute("SELECT count(*) FROM trader_realized").fetchone()
    print(f"trader_realized: {n} traders with at least one closed trade")
    return n


def buckets(con):
    rows = ",".join(
        f"({i}, '{label}', {lo if lo != float('-inf') else '-1e308'}, "
        f"{hi if hi != float('inf') else '1e308'})"
        for i, (label, lo, hi) in enumerate(config.PNL_BUCKETS)
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE realized_buckets AS
        WITH b(ord, label, lo, hi) AS (VALUES {rows})
        SELECT b.ord, b.label AS bucket, count(p.trader) AS traders,
               round(coalesce(sum(p.realized_pnl), 0), 2) AS bucket_realized_pnl
        FROM b LEFT JOIN trader_realized p
          ON p.realized_pnl >= b.lo AND p.realized_pnl < b.hi
        GROUP BY 1, 2 ORDER BY 1
    """)
    df = con.execute("""
        SELECT bucket, traders,
               round(100.0 * traders / nullif(sum(traders) OVER (), 0), 2) AS pct,
               bucket_realized_pnl
        FROM realized_buckets ORDER BY ord
    """).df()
    print("\n" + "=" * 64)
    print("REALIZED-ONLY PnL DISTRIBUTION (closed trades)")
    print("=" * 64)
    print(df.to_string(index=False))
    tot, win, lose, net = con.execute("""
        SELECT count(*), count(*) FILTER (realized_pnl > 0),
               count(*) FILTER (realized_pnl < 0), round(sum(realized_pnl), 2)
        FROM trader_realized
    """).fetchone()
    print(f"\n{tot} traders | {win} up ({win/max(tot,1)*100:.1f}%) | "
          f"{lose} down ({lose/max(tot,1)*100:.1f}%) | aggregate realized ${net:,.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true",
                    help="work on a copy, so this can run while another stage writes")
    args = ap.parse_args()

    if args.snapshot:
        snap = config.DATA_DIR / "pnl_snapshot.duckdb"
        shutil.copy(config.DB_PATH, snap)
        print(f"snapshot -> {snap}")
        con = duckdb.connect(str(snap))
    else:
        con = store.connect()

    build(con)
    buckets(con)
    for t in ("trader_realized", "realized_buckets"):
        path = config.DATA_DIR / f"{t}.csv"
        con.execute(f"COPY {t} TO '{path}' (HEADER, DELIMITER ',')")
        print(f"  exported {path}")
    con.close()


if __name__ == "__main__":
    main()
