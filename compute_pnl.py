#!/usr/bin/env python3
"""Stage 3: pool filtering, USD valuation, cost basis, PnL, bucket counts.

    python compute_pnl.py --stats                 # inspect distributions, tune filters
    python compute_pnl.py --max-legs 8            # full run

All aggregation is SQL against DuckDB. Inputs: `swap_legs` (stage 2),
`sol_hourly` + `token_prices` (prices.py). Outputs: `traders`, `swaps_usd`,
`cost_basis`, `trader_pnl`, `pnl_buckets`, all exported to CSV.
"""
import argparse

import config
import prices
import store

# An account is treated as a pool/router, not a trader, when it appears in many
# legs AND its buys and sells are roughly balanced -- that is what market making
# looks like. A retail FOMO trader is lopsided and infrequent.
# Three conditions must hold together. Any one alone misfires: a real trader can
# be frequent, a real trader can be balanced, and a real trader can DCA into a
# single mint -- but a market maker is all three at once.
DEFAULT_MAX_LEGS = 20      # below this, frequency says nothing
DEFAULT_BALANCE = 0.50     # pools take both sides; retail is lopsided
DEFAULT_LPM = 10.0         # legs per distinct mint: pools recycle one token


def show_stats(con):
    print("\n--- leg counts per account ---")
    print(con.execute("""
        SELECT legs_bucket, count(*) AS accounts, sum(legs) AS total_legs FROM (
          SELECT account, count(*) AS legs,
                 CASE WHEN count(*) = 1 THEN '1'
                      WHEN count(*) = 2 THEN '2'
                      WHEN count(*) <= 4 THEN '3-4'
                      WHEN count(*) <= 8 THEN '5-8'
                      WHEN count(*) <= 20 THEN '9-20'
                      WHEN count(*) <= 100 THEN '21-100'
                      ELSE '100+' END AS legs_bucket
          FROM swap_legs GROUP BY account)
        GROUP BY 1 ORDER BY min(legs)
    """).df().to_string(index=False))

    print("\n--- top accounts by leg count ---")
    print(con.execute("""
        SELECT account, count(*) legs,
               sum(side='BUY')::INT buys, sum(side='SELL')::INT sells,
               count(DISTINCT token_mint) mints
        FROM swap_legs GROUP BY 1 ORDER BY legs DESC LIMIT 10
    """).df().to_string(index=False))

    print("\n--- cash legs ---")
    print(con.execute("""
        SELECT cash_symbol, count(*) legs, round(sum(cash_abs), 2) total_cash
        FROM swap_legs GROUP BY 1 ORDER BY legs DESC
    """).df().to_string(index=False))


def build_traders(con, max_legs, balance, lpm=DEFAULT_LPM):
    con.execute(f"""
        CREATE OR REPLACE TABLE account_stats AS
        SELECT account,
               count(*)                        AS legs,
               count(DISTINCT signature)       AS txs,
               count(DISTINCT token_mint)      AS mints,
               sum(side = 'BUY')::BIGINT       AS buys,
               sum(side = 'SELL')::BIGINT      AS sells,
               min(block_time)                 AS first_seen,
               max(block_time)                 AS last_seen
        FROM swap_legs GROUP BY 1
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE traders AS
        SELECT *,
               CASE WHEN greatest(buys, sells) = 0 THEN 0.0
                    ELSE least(buys, sells)::DOUBLE / greatest(buys, sells) END AS balance_ratio
        FROM account_stats
        WHERE NOT (legs >= {max_legs}
                   AND least(buys, sells)::DOUBLE / nullif(greatest(buys, sells), 0) >= {balance}
                   AND legs::DOUBLE / nullif(mints, 0) >= {lpm})
    """)
    kept, dropped = con.execute("""
        SELECT (SELECT count(*) FROM traders),
               (SELECT count(*) FROM account_stats) - (SELECT count(*) FROM traders)
    """).fetchone()
    legs_kept, legs_all = con.execute("""
        SELECT (SELECT sum(legs) FROM traders), (SELECT sum(legs) FROM account_stats)
    """).fetchone()
    print(f"traders: {kept} kept, {dropped} excluded as pools/routers "
          f"(legs>={max_legs} AND balance>={balance} AND legs/mint>={lpm}); "
          f"legs {legs_kept}/{legs_all} retained ({legs_kept/legs_all*100:.1f}%)")


def build_swaps_usd(con):
    """Value each leg by its cash side: stables 1:1, SOL at the trade-hour price."""
    con.execute("""
        CREATE OR REPLACE TABLE swaps_usd AS
        SELECT l.signature, l.block_time, l.source, l.account AS trader,
               l.token_mint, l.token_abs, l.side, l.cash_symbol, l.cash_abs,
               CASE WHEN l.cash_symbol IN ('USDC', 'USDT') THEN l.cash_abs
                    WHEN s.price_usd IS NOT NULL       THEN l.cash_abs * s.price_usd
               END AS usd
        FROM swap_legs l
        JOIN traders t ON t.account = l.account
        LEFT JOIN sol_hourly s ON s.hour_ts = l.block_time - (l.block_time % 3600)
    """)
    total, unpriced = con.execute(
        "SELECT count(*), count(*) FILTER (usd IS NULL) FROM swaps_usd").fetchone()
    print(f"swaps_usd: {total} legs, {unpriced} unpriced "
          f"({unpriced / max(total, 1) * 100:.2f}% - SOL legs outside the price series)")
    con.execute("DELETE FROM swaps_usd WHERE usd IS NULL")


def build_cost_basis(con):
    con.execute("""
        CREATE OR REPLACE TABLE cost_basis AS
        WITH agg AS (
          SELECT trader, token_mint,
                 sum(CASE WHEN side = 'BUY'  THEN token_abs ELSE 0 END) AS total_bought,
                 sum(CASE WHEN side = 'SELL' THEN token_abs ELSE 0 END) AS total_sold,
                 sum(CASE WHEN side = 'BUY'  THEN usd ELSE 0 END)       AS total_buy_usd,
                 sum(CASE WHEN side = 'SELL' THEN usd ELSE 0 END)       AS total_sell_usd
          FROM swaps_usd GROUP BY 1, 2
        )
        SELECT *,
               total_buy_usd / nullif(total_bought, 0)      AS avg_cost,
               greatest(total_bought - total_sold, 0)       AS remaining_qty,
               -- Sells can exceed in-window buys when the position predates the
               -- window. Only the matched part has a known cost basis.
               least(total_sold, total_bought)              AS matched_sold_qty,
               total_sold > total_bought                    AS has_unmatched_sells,
               -- No in-window buy means no cost basis at all: the position was
               -- opened before the window. Scoring those as $0 PnL would be a
               -- fabricated zero, so they are excluded and counted instead.
               total_bought > 0                             AS basis_known
        FROM agg
    """)
    n, pos, unmatched, nobasis = con.execute("""
        SELECT count(*), count(*) FILTER (remaining_qty > 0),
               count(*) FILTER (has_unmatched_sells AND basis_known),
               count(*) FILTER (NOT basis_known) FROM cost_basis
    """).fetchone()
    print(f"cost_basis: {n} trader-token rows, {pos} with open inventory, "
          f"{unmatched} with partial pre-window basis, "
          f"{nobasis} sell-only with no in-window basis (excluded from PnL)")


def build_pnl(con):
    con.execute("""
        CREATE OR REPLACE TABLE token_pnl AS
        SELECT c.trader, c.token_mint, c.remaining_qty, c.avg_cost,
               p.price_usd,
               -- realized: proceeds on the matched quantity, less its cost
               CASE WHEN c.total_sold = 0 THEN 0.0
                    ELSE c.total_sell_usd * (c.matched_sold_qty / c.total_sold)
                         - c.matched_sold_qty * coalesce(c.avg_cost, 0)
               END AS realized_pnl,
               CASE WHEN c.remaining_qty > 0
                    THEN c.remaining_qty * coalesce(p.price_usd, 0)
                         - c.remaining_qty * coalesce(c.avg_cost, 0)
                    ELSE 0.0 END AS unrealized_pnl
        FROM cost_basis c
        LEFT JOIN token_prices p ON p.mint = c.token_mint
        WHERE c.basis_known
    """)
    con.execute("""
        CREATE OR REPLACE TABLE trader_pnl AS
        SELECT t.trader,
               sum(t.realized_pnl)   AS realized_pnl,
               sum(t.unrealized_pnl) AS unrealized_pnl,
               sum(t.realized_pnl) + sum(t.unrealized_pnl) AS net_pnl,
               count(*)              AS tokens_traded
        FROM token_pnl t GROUP BY 1
    """)
    n, excl = con.execute("""
        SELECT (SELECT count(*) FROM trader_pnl),
               (SELECT count(DISTINCT trader) FROM cost_basis WHERE NOT basis_known
                  AND trader NOT IN (SELECT trader FROM trader_pnl))
    """).fetchone()
    print(f"trader_pnl: {n} traders scored; {excl} traders dropped entirely "
          f"(every position opened before the window)")


def build_buckets(con):
    rows = ",".join(
        f"({i}, '{label}', {lo if lo != float('-inf') else '-1e308'}, "
        f"{hi if hi != float('inf') else '1e308'})"
        for i, (label, lo, hi) in enumerate(config.PNL_BUCKETS)
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE pnl_buckets AS
        WITH b(ord, label, lo, hi) AS (VALUES {rows})
        SELECT b.ord, b.label AS bucket,
               count(p.trader) AS traders,
               round(coalesce(sum(p.net_pnl), 0), 2) AS bucket_net_pnl
        FROM b LEFT JOIN trader_pnl p ON p.net_pnl >= b.lo AND p.net_pnl < b.hi
        GROUP BY 1, 2 ORDER BY 1
    """)


def report(con):
    df = con.execute("""
        SELECT bucket, traders,
               round(100.0 * traders / nullif(sum(traders) OVER (), 0), 2) AS pct,
               bucket_net_pnl
        FROM pnl_buckets ORDER BY ord
    """).df()
    print("\n" + "=" * 62)
    print("PnL DISTRIBUTION")
    print("=" * 62)
    print(df.to_string(index=False))
    tot, win, lose, net = con.execute("""
        SELECT count(*), count(*) FILTER (net_pnl > 0), count(*) FILTER (net_pnl < 0),
               round(sum(net_pnl), 2) FROM trader_pnl
    """).fetchone()
    print(f"\n{tot} traders | {win} up ({win/max(tot,1)*100:.1f}%) | "
          f"{lose} down ({lose/max(tot,1)*100:.1f}%) | aggregate net ${net:,.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="print distributions and exit")
    ap.add_argument("--max-legs", type=int, default=DEFAULT_MAX_LEGS)
    ap.add_argument("--balance", type=float, default=DEFAULT_BALANCE)
    ap.add_argument("--legs-per-mint", type=float, default=DEFAULT_LPM)
    ap.add_argument("--skip-prices", action="store_true")
    args = ap.parse_args()

    con = store.connect()
    if args.stats:
        show_stats(con)
        con.close()
        return

    build_traders(con, args.max_legs, args.balance, args.legs_per_mint)
    if not args.skip_prices:
        prices.load_sol_hourly(con)
    build_swaps_usd(con)
    build_cost_basis(con)
    if not args.skip_prices:
        mints = [r[0] for r in con.execute(
            "SELECT DISTINCT token_mint FROM cost_basis WHERE remaining_qty > 0").fetchall()]
        print(f"pricing {len(mints)} mints with open inventory...")
        prices.load_token_prices(con, mints)
    build_pnl(con)
    build_buckets(con)
    report(con)

    for t in ("traders", "cost_basis", "trader_pnl", "pnl_buckets"):
        print(f"  exported {store.export_csv(con, t)}")
    con.close()


if __name__ == "__main__":
    main()
