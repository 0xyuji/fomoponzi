"""DuckDB access. One file at data/pnl.duckdb; every stage reads/writes tables here."""
import duckdb

import config


def connect(read_only: bool = False):
    return duckdb.connect(str(config.DB_PATH), read_only=read_only)


def init_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS vault_signatures (
            signature   VARCHAR PRIMARY KEY,
            slot        BIGINT,
            block_time  BIGINT,
            err         VARCHAR
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS traders (
            trader      VARCHAR PRIMARY KEY,
            first_seen  BIGINT,
            last_seen   BIGINT,
            tx_count    BIGINT
        );
    """)


def export_csv(con, table: str, path=None):
    path = path or (config.DATA_DIR / f"{table}.csv")
    con.execute(f"COPY {table} TO '{path}' (HEADER, DELIMITER ',')")
    return path
