# fomoponzi

Trader profitability on the FOMO app, measured from Solana chain data instead of a Dune dashboard.

This pulls every transaction that touched the app's fee wallet over 30 days, reconstructs
each trader's swaps, computes cost basis and PnL per wallet, and buckets the results.
Everything runs on a free Helius RPC key and the free GeckoTerminal price API.

## Results

30 day window ending 2026-09-05. 36,307 traders scored.

| bucket | traders | % | net PnL |
|---|---|---|---|
| < -$10K | 160 | 0.44 | -2,922,801 |
| -$10K to -$5K | 437 | 1.20 | -2,955,060 |
| -$5K to -$1K | 3,937 | 10.84 | -7,971,156 |
| -$1K to -$500 | 4,301 | 11.85 | -3,055,733 |
| -$500 to -$100 | 14,930 | 41.12 | -3,836,316 |
| -$100 to $0 | 6,550 | 18.04 | -269,856 |
| $0 to $100 | 2,556 | 7.04 | 81,788 |
| $100 to $500 | 1,731 | 4.77 | 422,784 |
| $500 to $1K | 548 | 1.51 | 387,041 |
| $1K to $5K | 812 | 2.24 | 1,836,913 |
| $5K to $10K | 161 | 0.44 | 1,100,132 |
| $10K+ | 184 | 0.51 | 7,558,007 |

83.5% of traders are down. The median trader is down $198. Only 3.2% have cleared $1,000,
and the top 10 wallets hold 26.5% of every dollar of profit in the dataset.

### Read this before quoting the headline number

The aggregate is -$9.6M, but almost none of it is banked. Split it out:

| | realized | unrealized |
|---|---|---|
| all traders | +$44,795 | -$9,669,051 |

Closed trades come out roughly even. The entire loss is mark to market on tokens people
are still holding. $1.16M of it sits in 5,086 positions whose token has no live pool left,
which get marked at zero.

The same assumption cuts both ways. The top wallet shows $959K of unrealized gain on
42 swaps and -$29 realized, which is a paper number on illiquid memecoins that thin
liquidity would not actually pay out. Treat both tails as soft. The shape of the
distribution holds in either cut. Realized only is still 45% winners to 55% losers.

A realized only view is in `results/realized_buckets.csv` and is the more defensible cut.

## How it works

    fetch_signatures.py     getSignaturesForAddress, paginated, 30 day cutoff
    fetch_transactions.py   Enhanced Transactions API, 100 signatures per call
    prices.py               hourly SOL/USD, plus current token prices
    compute_pnl.py          pool filter, cost basis, realized + unrealized, buckets
    compute_realized.py     realized only, no price dependency

Each stage writes to DuckDB and caches raw API responses to disk, so you can rerun any
stage without spending calls. All aggregation is SQL.

    pip install -r requirements.txt
    cp .env.example .env        # add your Helius key
    python fetch_signatures.py
    python fetch_transactions.py
    python compute_pnl.py

## Things worth knowing if you rerun this

**Helius returns no USD amounts and no parsed swap events for these DEX sources.**
`events.swap` was empty on every transaction sampled. Swap legs are derived from the net
token and native balance change per account instead, which works regardless of source.
Each swap is then valued by its cash side. USDC and USDT are 1:1, SOL is priced at the
trade hour rather than at today's price. SOL moved 39% inside this window, so using a
current price would have thrown the numbers off badly.

**feePayer is not the trader.** One relayer paid for 82% of the transactions sampled.
Trader identity has to come from balance changes.

**Pool accounts look exactly like traders.** Both show a cash leg and a token leg with
opposite signs. The filter removes an account only if all three of these hold at once:
20 or more legs, buy/sell balance of 0.5 or higher, and 10 or more legs per distinct
token. Each condition alone catches real people, since a retail trader can be frequent,
can be balanced, and can buy the same token repeatedly. Only a market maker is all three.
That removed 1,553 accounts holding 48.7% of all swap legs. The most obvious one had
11,300 swaps on a single token.

**Sell only positions have no cost basis.** If someone bought before the window and sold
inside it, there is no buy price to measure against. 15,282 traders were dropped for this.
Scoring them at $0 instead was a bug that parked 40% of traders at break even and made the
distribution look completely different. They are probably long term holders, which means
excluding them likely flatters the result rather than the reverse.

**Rate limits.** The free Helius tier throttles harder than its documented 2 req/s on the
Enhanced API, and GeckoTerminal returns `Retry-After: 0` on a 429, so backoff has to ignore
that header when it would shorten the wait.

## Numbers behind the run

    193,518   signatures fetched
    190,958   transactions parsed, 0 failed
    483,035   swap legs derived
     51,589   counterparty wallets
      1,553   removed as pools and routers
     15,282   dropped for no in window cost basis
     36,307   traders scored

## Scope

This measures trades routed through the app's fee wallet, so it is app activity, not each
trader's full 30 day history elsewhere. For "how are people doing on FOMO" that is the
right denominator. It is not their total trading PnL.

The repo name is a joke about the outcome distribution. Nothing here shows a Ponzi
structure, and nothing here tests for one. It shows that most people lose money trading
memecoins, which is also true off this app.

## Files

    results/pnl_buckets.csv        bucket counts, net PnL
    results/realized_buckets.csv   bucket counts, realized only
    results/trader_pnl.csv         per trader realized, unrealized, net
    results/trader_realized.csv    per trader closed trade PnL

Raw caches and the DuckDB file are not committed. They come to about 640MB.
