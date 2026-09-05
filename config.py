"""Shared config. Reads .env, defines constants used by every stage."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
if not HELIUS_API_KEY:
    raise SystemExit(
        "HELIUS_API_KEY is not set. Copy .env.example to .env and fill it in."
    )

VAULT_ADDRESS = os.getenv("VAULT_ADDRESS", "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "30"))

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
ENHANCED_TX_URL = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}"

# Free-tier rate limits.
RPC_RATE_LIMIT = 10.0        # requests/second
ENHANCED_RATE_LIMIT = 2.0    # requests/second
GECKO_RATE_LIMIT = 0.35      # requests/second; GT free tier 429s well before its documented 30/min

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
RPC_SIG_CACHE = CACHE_DIR / "rpc_sigs"
ENHANCED_CACHE = CACHE_DIR / "enhanced"
PRICE_CACHE = CACHE_DIR / "prices"
DB_PATH = DATA_DIR / "pnl.duckdb"

for _d in (DATA_DIR, CACHE_DIR, RPC_SIG_CACHE, ENHANCED_CACHE, PRICE_CACHE):
    _d.mkdir(parents=True, exist_ok=True)

# Tokens treated as "cash" -- the other leg of a swap, never a position.
CASH_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}

PNL_BUCKETS = [
    ("< -$10K",        float("-inf"), -10_000),
    ("-$10K to -$5K",  -10_000,        -5_000),
    ("-$5K to -$1K",    -5_000,        -1_000),
    ("-$1K to -$500",   -1_000,          -500),
    ("-$500 to -$100",    -500,          -100),
    ("-$100 to $0",       -100,             0),
    ("$0 to $100",           0,           100),
    ("$100 to $500",       100,           500),
    ("$500 to $1K",        500,         1_000),
    ("$1K to $5K",       1_000,         5_000),
    ("$5K to $10K",      5_000,        10_000),
    ("$10K+",           10_000, float("inf")),
]

# Helius returns no USD amounts, so swaps are valued by their cash leg.
# USDC/USDT legs are USD 1:1; SOL legs need a historical SOL/USD series.
SOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
