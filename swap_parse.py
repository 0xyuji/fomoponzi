"""Turn a Helius Enhanced transaction into swap legs.

Helius populates `events.swap` for only some DEX sources -- for this vault it is
empty on every sampled transaction -- so legs are derived from the net token and
native balance change per account, which works regardless of source.
"""
import config

LAMPORTS = 1_000_000_000


def net_deltas(tx: dict) -> dict:
    """{account: {mint: signed_delta}} from token + native transfers."""
    net: dict = {}

    def bump(acct, mint, amt):
        if not acct:
            return
        net.setdefault(acct, {})
        net[acct][mint] = net[acct].get(mint, 0.0) + amt

    for t in tx.get("tokenTransfers") or []:
        amt = t.get("tokenAmount") or 0.0
        bump(t.get("fromUserAccount"), t["mint"], -amt)
        bump(t.get("toUserAccount"), t["mint"], amt)

    for n in tx.get("nativeTransfers") or []:
        amt = (n.get("amount") or 0) / LAMPORTS
        bump(n.get("fromUserAccount"), config.SOL_MINT, -amt)
        bump(n.get("toUserAccount"), config.SOL_MINT, amt)

    # drop dust from rounding
    for acct in list(net):
        net[acct] = {m: v for m, v in net[acct].items() if abs(v) > 1e-12}
        if not net[acct]:
            del net[acct]
    return net


def swap_legs(tx: dict):
    """Yield one row per account that swapped cash <-> a non-cash token.

    A leg is any account holding both a cash delta and a non-cash delta of the
    opposite sign. That shape matches real traders AND AMM pools/routers -- pools
    are stripped later by interaction frequency, in SQL.
    """
    ts = tx.get("timestamp")
    sig = tx.get("signature")
    source = tx.get("source")

    for acct, deltas in net_deltas(tx).items():
        if acct == config.VAULT_ADDRESS:
            continue
        cash = {m: v for m, v in deltas.items() if m in config.CASH_MINTS}
        toks = {m: v for m, v in deltas.items() if m not in config.CASH_MINTS}
        if not cash or not toks:
            continue

        # dominant leg on each side
        cash_mint, cash_delta = max(cash.items(), key=lambda kv: abs(kv[1]))
        tok_mint, tok_delta = max(toks.items(), key=lambda kv: abs(kv[1]))
        if cash_delta * tok_delta >= 0:
            continue  # same sign -> not a swap (deposit, LP add, etc.)

        yield {
            "signature": sig,
            "block_time": ts,
            "source": source,
            "account": acct,
            "token_mint": tok_mint,
            "token_delta": tok_delta,
            "cash_mint": cash_mint,
            "cash_symbol": config.CASH_MINTS[cash_mint],
            "cash_delta": cash_delta,
            "side": "BUY" if tok_delta > 0 else "SELL",
            "cash_abs": abs(cash_delta),
            "token_abs": abs(tok_delta),
        }
