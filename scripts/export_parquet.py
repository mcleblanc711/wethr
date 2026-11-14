#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1"]
# ///
"""Export a consistent snapshot of Wethr's SQLite DB to typed Parquet files.

Usage (from the repo root):
    uv run scripts/export_parquet.py [--db data/wethr.db] [--out ~/wethr-export]
        [--reuse-snapshot] [--tables t1,t2]

Steps:
  1. `VACUUM INTO` the live DB to <out>/wethr-snapshot.db (consistent even
     while the collector writes; the live DB is only read).
  2. DuckDB reads the snapshot and writes <out>/parquet/<table>.parquet (zstd).
     ISO-8601 text columns named *_at / target_date / date are cast to
     TIMESTAMPTZ / DATE when every non-null value parses; otherwise they stay
     text and a warning is printed. Nothing is silently nulled.
  3. Row counts are compared SQLite vs Parquet; exit 1 on any mismatch.

Base tables only: views (e.g. paper_trades_compatible) are left to the dbt layer.
Empty tables are skipped.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import duckdb

DATE_COLUMNS = {"target_date", "date"}


def is_timestamp_column(name: str) -> bool:
    return name.endswith("_at")


def take_snapshot(db: Path, snapshot: Path) -> None:
    if shutil.which("sqlite3") is None:
        sys.exit("sqlite3 CLI not found; it is needed for a consistent .backup")
    tmp = snapshot.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    print(f"snapshot {db} -> {snapshot} (this reads the whole DB; a few minutes)", flush=True)
    started = time.monotonic()
    # VACUUM INTO reads inside one transaction, so with the live DB in WAL mode
    # the copy is consistent and never restarts when the collector writes
    # (the online-backup API behind `.backup` does restart on each write).
    escaped = str(tmp).replace("'", "''")
    subprocess.run(
        ["sqlite3", "-readonly", str(db), ".timeout 60000", f"VACUUM INTO '{escaped}'"],
        check=True,
    )
    tmp.replace(snapshot)
    print(f"snapshot done in {time.monotonic() - started:.0f}s")


def list_tables(snapshot: Path) -> list[str]:
    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows]


def quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def build_select(
    con: duckdb.DuckDBPyConnection, staged: Path, table: str
) -> tuple[str, list[str]]:
    """SELECT over the staged (all-as-loaded) Parquet that casts time columns.

    A column is cast only when every non-null value parses; the check is one
    columnar pass over the staged file rather than a scan of SQLite per column.
    """
    src = f"read_parquet('{staged}')"
    cols = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
    candidates: list[tuple[str, str]] = []
    for name, dtype, *_ in cols:
        if dtype != "VARCHAR":
            continue
        if name in DATE_COLUMNS:
            candidates.append((name, "DATE"))
        elif is_timestamp_column(name):
            candidates.append((name, "TIMESTAMPTZ"))
    bad: dict[str, int] = {}
    if candidates:
        checks = ", ".join(
            f"count_if({quote(n)} IS NOT NULL AND TRY_CAST({quote(n)} AS {t}) IS NULL)"
            for n, t in candidates
        )
        counts = con.execute(f"SELECT {checks} FROM {src}").fetchone()
        bad = {n: c for (n, _), c in zip(candidates, counts)}
    targets = dict(candidates)
    exprs: list[str] = []
    warnings: list[str] = []
    for name, *_ in cols:
        q = quote(name)
        if name not in targets:
            exprs.append(q)
        elif bad[name]:
            warnings.append(f"{table}.{name}: {bad[name]} values don't parse as {targets[name]}; kept as text")
            exprs.append(q)
        else:
            exprs.append(f"CAST({q} AS {targets[name]}) AS {q}")
    return "SELECT " + ", ".join(exprs) + f" FROM {src}", warnings


def sqlite_count(snapshot: Path, table: str) -> int:
    # SQLite answers COUNT(*) from the narrowest index, far cheaper than a full scan.
    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0]


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=repo / "data" / "wethr.db")
    ap.add_argument("--out", type=Path, default=Path.home() / "wethr-export")
    ap.add_argument("--reuse-snapshot", action="store_true", help="skip step 1 if a snapshot exists")
    ap.add_argument("--tables", help="comma-separated subset of tables to export")
    args = ap.parse_args()

    out: Path = args.out.expanduser()
    parquet_dir = out / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    snapshot = out / "wethr-snapshot.db"

    if args.reuse_snapshot and snapshot.exists():
        print(f"reusing snapshot {snapshot}")
    else:
        take_snapshot(args.db, snapshot)

    tables = list_tables(snapshot)
    if args.tables:
        wanted = set(args.tables.split(","))
        unknown = wanted - set(tables)
        if unknown:
            sys.exit(f"unknown tables: {', '.join(sorted(unknown))}")
        tables = [t for t in tables if t in wanted]

    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{snapshot}' AS src (TYPE sqlite, READ_ONLY)")
    con.execute("SET TimeZone = 'UTC'")

    results: list[tuple[str, int, int, int]] = []
    all_warnings: list[str] = []
    mismatches: list[str] = []
    for table in tables:
        src_rows = sqlite_count(snapshot, table)
        dest = parquet_dir / f"{table}.parquet"
        if src_rows == 0:
            print(f"skip   {table} (empty)", flush=True)
            dest.unlink(missing_ok=True)
            continue
        print(f"export {table} ({src_rows:,} rows)", flush=True)
        started = time.monotonic()
        # Pass 1: the only read of SQLite. Pass 2: typed rewrite from Parquet.
        staged = dest.with_suffix(".staged.parquet")
        tmp = dest.with_suffix(".parquet.tmp")
        con.execute(f"COPY (SELECT * FROM src.{quote(table)}) TO '{staged}' (FORMAT parquet, COMPRESSION zstd)")
        select, warnings = build_select(con, staged, table)
        all_warnings += warnings
        con.execute(f"COPY ({select}) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 500000)")
        staged.unlink()
        tmp.replace(dest)
        pq_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{dest}')").fetchone()[0]
        if pq_rows != src_rows:
            mismatches.append(f"{table}: sqlite={src_rows} parquet={pq_rows}")
        results.append((table, src_rows, pq_rows, dest.stat().st_size))
        print(f"       done in {time.monotonic() - started:.0f}s", flush=True)

    print(f"\n{'table':<26}{'sqlite rows':>14}{'parquet rows':>14}{'MB':>10}")
    for table, s, p, size in results:
        print(f"{table:<26}{s:>14,}{p:>14,}{size / 1e6:>10.1f}")
    total = sum(r[3] for r in results)
    print(f"{'TOTAL':<26}{'':>28}{total / 1e6:>10.1f}")

    for w in all_warnings:
        print(f"WARNING {w}")
    if mismatches:
        for m in mismatches:
            print(f"MISMATCH {m}")
        return 1
    print(f"\nOK: {len(results)} tables in {parquet_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
