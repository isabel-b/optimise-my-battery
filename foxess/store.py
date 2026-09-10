"""SQLite storage for FoxESS data.

Every raw API response is kept in `raw_responses` so that if a parser turns out
to be wrong we can re-parse without spending API quota. Parsed data lands in
`history` (5-minute samples) and `report` (energy totals per hour/day/month).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "foxess.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    sn TEXT PRIMARY KEY,
    device_type TEXT,
    station_name TEXT,
    has_battery INTEGER,
    has_pv INTEGER,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    sn TEXT,
    key TEXT,                -- e.g. the day or period requested
    payload TEXT NOT NULL,
    UNIQUE(endpoint, sn, key)
);
CREATE TABLE IF NOT EXISTS history (
    sn TEXT NOT NULL,
    variable TEXT NOT NULL,
    ts TEXT NOT NULL,        -- local time 'YYYY-MM-DD HH:MM:SS' as reported by FoxCloud
    value REAL,
    unit TEXT,
    PRIMARY KEY (sn, variable, ts)
);
CREATE INDEX IF NOT EXISTS history_ts ON history (sn, ts);
CREATE TABLE IF NOT EXISTS report (
    sn TEXT NOT NULL,
    dimension TEXT NOT NULL, -- 'day' (hourly values), 'month' (daily), 'year' (monthly)
    period TEXT NOT NULL,    -- start of the bucket, ISO local
    variable TEXT NOT NULL,
    value REAL,
    unit TEXT,
    PRIMARY KEY (sn, dimension, period, variable)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


# ---- raw cache --------------------------------------------------------------
def cached(conn: sqlite3.Connection, endpoint: str, sn: str | None, key: str) -> Any | None:
    row = conn.execute(
        "SELECT payload FROM raw_responses WHERE endpoint=? AND sn IS ? AND key=?",
        (endpoint, sn, key),
    ).fetchone()
    return json.loads(row[0]) if row else None


def cache(conn: sqlite3.Connection, endpoint: str, sn: str | None, key: str, payload: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO raw_responses (fetched_at, endpoint, sn, key, payload) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), endpoint, sn, key, json.dumps(payload)),
    )
    conn.commit()


# ---- devices ---------------------------------------------------------------
def upsert_devices(conn: sqlite3.Connection, devices: Iterable[dict]) -> None:
    for d in devices:
        conn.execute(
            "INSERT OR REPLACE INTO devices (sn, device_type, station_name, has_battery, has_pv, raw) VALUES (?,?,?,?,?,?)",
            (d.get("deviceSN"), d.get("deviceType"), d.get("stationName"),
             int(bool(d.get("hasBattery"))), int(bool(d.get("hasPV"))), json.dumps(d)),
        )
    conn.commit()


# ---- history ---------------------------------------------------------------
_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def normalise_time(s: str) -> str:
    """FoxCloud returns e.g. '2024-01-17 12:05:00 GMT+0000' or an ISO string. Keep local wall time."""
    m = _TIME_RE.match(s.strip())
    if not m:
        raise ValueError(f"unrecognised time string: {s!r}")
    return f"{m.group(1)} {m.group(2)}"


def insert_history(conn: sqlite3.Connection, sn: str, datas: list[dict]) -> int:
    rows = []
    for series in datas:
        var = series.get("variable")
        unit = series.get("unit")
        for point in series.get("data", []):
            if point.get("value") is None:
                continue
            rows.append((sn, var, normalise_time(point["time"]), float(point["value"]), unit))
    conn.executemany(
        "INSERT OR REPLACE INTO history (sn, variable, ts, value, unit) VALUES (?,?,?,?,?)", rows
    )
    conn.commit()
    return len(rows)


# ---- report ----------------------------------------------------------------
def parse_report(result: Any, dimension: str, year: int, month: int, day: int) -> list[tuple[str, str, float, str | None]]:
    """Return (period_iso, variable, value, unit) tuples from either known response shape.

    Shape A (docs):   {"data": [{"variable", "unit", "data": [{"time", "value"}]}]}
    Shape B (seen in the wild): [{"variable", "unit", "values": [v1, v2, ...]}]  index -> hour/day/month
    """
    series_list = result.get("data", []) if isinstance(result, dict) else (result or [])
    out: list[tuple[str, str, float, str | None]] = []
    for series in series_list:
        var, unit = series.get("variable"), series.get("unit")
        if "data" in series:
            for p in series["data"]:
                if p.get("value") is None:
                    continue
                out.append((normalise_time(p["time"]), var, float(p["value"]), unit))
        elif "values" in series:
            for i, v in enumerate(series["values"]):
                if v is None:
                    continue
                if dimension == "day":
                    period = f"{year:04d}-{month:02d}-{day:02d} {i:02d}:00:00"
                elif dimension == "month":
                    period = f"{year:04d}-{month:02d}-{i + 1:02d} 00:00:00"
                else:
                    period = f"{year:04d}-{i + 1:02d}-01 00:00:00"
                out.append((period, var, float(v), unit))
    return out


def insert_report(conn: sqlite3.Connection, sn: str, dimension: str,
                  rows: Iterable[tuple[str, str, float, str | None]]) -> int:
    rows = [(sn, dimension, period, var, val, unit) for period, var, val, unit in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO report (sn, dimension, period, variable, value, unit) VALUES (?,?,?,?,?,?)", rows
    )
    conn.commit()
    return len(rows)


# ---- read side ---------------------------------------------------------------
def history_days_present(conn: sqlite3.Connection, sn: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT key FROM raw_responses WHERE endpoint='history' AND sn=?", (sn,)
    )}
