from datetime import date, timedelta
from pathlib import Path

import duckdb

TABLES = ("sessions", "events", "orders")


def day_file(data_dir: Path, table: str, day: date) -> Path:
    return Path(data_dir) / table / f"{day}.parquet"


def available_days(data_dir: Path) -> list[date]:
    return sorted(date.fromisoformat(p.stem) for p in (Path(data_dir) / "orders").glob("*.parquet"))


def history_days(data_dir: Path, day: date, n: int = 14) -> list[date]:
    """Up to n most recent days before `day` that have all three table files."""
    out = []
    for i in range(1, 60):
        d = day - timedelta(days=i)
        if all(day_file(data_dir, t, d).exists() for t in TABLES):
            out.append(d)
            if len(out) == n:
                break
    return out


def load_day(con: duckdb.DuckDBPyConnection, data_dir: Path, day: date, prefix: str = "") -> None:
    """Register `<prefix>sessions|events|orders` views for one day's snapshot."""
    for t in TABLES:
        f = day_file(data_dir, t, day).as_posix()
        con.execute(f"create or replace view {prefix}{t} as select * from read_parquet('{f}')")
