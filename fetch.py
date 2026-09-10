#!/usr/bin/env python3
"""Pull data from FoxCloud into data/foxess.sqlite.

Usage:
  python fetch.py devices                 # list inverters on the account
  python fetch.py history --days 30       # 5-min samples, one API call per day (skips days already cached)
  python fetch.py report --months 6       # daily energy totals per month, plus monthly totals for the year
  python fetch.py real                    # print live values
  python fetch.py schedule                # print the inverter's current work-mode time segments (read only)
  python fetch.py status                  # what is in the database
  python fetch.py reparse                 # rebuild history/report tables from cached raw responses

Quota: 1,440 calls per inverter per day. `history --days 90` costs 90 calls.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from foxess import store
from foxess.client import FoxApiError, FoxClient, load_client_from_env


def pick_sn(conn, client: FoxClient, preferred: str | None) -> str:
    if preferred:
        return preferred
    row = conn.execute("SELECT sn FROM devices ORDER BY has_battery DESC LIMIT 1").fetchone()
    if row:
        return row[0]
    devices = client.device_list()
    if not devices:
        raise SystemExit("No devices found on this account.")
    store.upsert_devices(conn, devices)
    return devices[0]["deviceSN"]


def cmd_devices(args, conn, client, preferred):
    devices = client.device_list()
    store.upsert_devices(conn, devices)
    for d in devices:
        status = {1: "online", 2: "fault", 3: "offline"}.get(d.get("status"), d.get("status"))
        print(f"{d.get('deviceSN')}  {d.get('deviceType')}  {d.get('stationName')}  "
              f"battery={d.get('hasBattery')} pv={d.get('hasPV')}  {status}")


def cmd_history(args, conn, client, preferred):
    sn = pick_sn(conn, client, preferred)
    today = date.today()
    days = [today - timedelta(days=i) for i in range(args.days)]
    have = store.history_days_present(conn, sn)
    # Always refresh today and yesterday: today is partial, yesterday may have been fetched early.
    todo = [d for d in days if d.isoformat() not in have or (today - d).days <= 1]
    print(f"{sn}: {len(days)} days requested, {len(todo)} to fetch ({len(days) - len(todo)} cached)")
    if len(todo) > 1200:
        raise SystemExit("That exceeds the daily API quota. Fetch in chunks of <1200 days.")
    total_rows = 0
    for i, d in enumerate(sorted(todo)):
        try:
            datas = client.history_day(sn, datetime(d.year, d.month, d.day))
        except FoxApiError as e:
            print(f"  {d}: {e}")
            if e.errno in (40401, 41809):
                break
            continue
        store.cache(conn, "history", sn, d.isoformat(), datas)
        n = store.insert_history(conn, sn, datas)
        total_rows += n
        print(f"  {d}: {n} samples  [{i + 1}/{len(todo)}]")
    print(f"done: {total_rows} rows, {client.calls_made} API calls")


def _month_iter(n: int):
    d = date.today().replace(day=1)
    for _ in range(n):
        yield d.year, d.month
        d = (d - timedelta(days=1)).replace(day=1)


def cmd_report(args, conn, client, preferred):
    sn = pick_sn(conn, client, preferred)
    today = date.today()
    for year, month in _month_iter(args.months):
        key = f"month:{year}-{month:02d}"
        current = (year, month) == (today.year, today.month)
        if not current and store.cached(conn, "report", sn, key) is not None:
            print(f"  {key}: cached")
            continue
        try:
            result = client.report(sn, "month", year, month, 1)
        except FoxApiError as e:
            print(f"  {key}: {e}")
            continue
        store.cache(conn, "report", sn, key, result)
        n = store.insert_report(conn, sn, "month", store.parse_report(result, "month", year, month, 1))
        print(f"  {key}: {n} values")
    for year in sorted({y for y, _ in _month_iter(args.months)}):
        key = f"year:{year}"
        try:
            result = client.report(sn, "year", year, 1, 1)
        except FoxApiError as e:
            print(f"  {key}: {e}")
            continue
        store.cache(conn, "report", sn, key, result)
        n = store.insert_report(conn, sn, "year", store.parse_report(result, "year", year, 1, 1))
        print(f"  {key}: {n} values")
    print(f"done: {client.calls_made} API calls")


def cmd_real(args, conn, client, preferred):
    sn = pick_sn(conn, client, preferred)
    for v in client.real_time(sn):
        print(f"{v.get('variable'):24s} {v.get('value')!s:>10} {v.get('unit') or ''}")


def cmd_schedule(args, conn, client, preferred):
    sn = pick_sn(conn, client, preferred)
    flag = client.scheduler_flag(sn)
    sched = client.scheduler_get(sn)
    store.cache(conn, "scheduler", sn, "latest", {"flag": flag, "schedule": sched})
    print(f"scheduler supported={flag.get('support')} enabled={flag.get('enable')}")
    for i, g in enumerate(sched.get("groups", [])):
        if not g.get("enable"):
            continue
        print(f"  [{i}] {g['startHour']:02d}:{g['startMinute']:02d}-{g['endHour']:02d}:{g['endMinute']:02d}  "
              f"{g['workMode']:<14} minSocOnGrid={g.get('minSocOnGrid')}%  fdSoc={g.get('fdSoc')}%  "
              f"fdPwr={g.get('fdPwr')}W  maxSoc={g.get('maxSoc')}%")
    modes = sched.get("properties", {}).get("workmode", {}).get("enumList")
    if modes:
        print("  work modes this inverter accepts:", ", ".join(modes))


def cmd_status(args, conn, client, preferred):
    for sn, dtype, name in conn.execute("SELECT sn, device_type, station_name FROM devices"):
        print(f"device {sn} ({dtype}, {name})")
    for sn, n, lo, hi in conn.execute(
        "SELECT sn, COUNT(*), MIN(ts), MAX(ts) FROM history GROUP BY sn"):
        days = conn.execute("SELECT COUNT(DISTINCT substr(ts,1,10)) FROM history WHERE sn=?", (sn,)).fetchone()[0]
        print(f"history {sn}: {n} samples over {days} days, {lo} .. {hi}")
    for sn, dim, n, lo, hi in conn.execute(
        "SELECT sn, dimension, COUNT(*), MIN(period), MAX(period) FROM report GROUP BY sn, dimension"):
        print(f"report  {sn} [{dim}]: {n} values, {lo} .. {hi}")
    n = conn.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]
    print(f"raw responses cached: {n}")


def cmd_reparse(args, conn, client, preferred):
    conn.execute("DELETE FROM history")
    conn.execute("DELETE FROM report")
    hist = rep = 0
    for sn, key, payload in conn.execute(
        "SELECT sn, key, payload FROM raw_responses WHERE endpoint='history'"):
        import json
        hist += store.insert_history(conn, sn, json.loads(payload))
    for sn, key, payload in conn.execute(
        "SELECT sn, key, payload FROM raw_responses WHERE endpoint='report'"):
        import json
        dim, period = key.split(":", 1)
        parts = [int(p) for p in period.split("-")] + [1, 1]
        rep += store.insert_report(conn, sn, dim, store.parse_report(json.loads(payload), dim, *parts[:3]))
    print(f"reparsed {hist} history rows, {rep} report rows")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sn", help="device serial number (overrides FOX_DEVICE_SN)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices").set_defaults(fn=cmd_devices)
    h = sub.add_parser("history"); h.add_argument("--days", type=int, default=30); h.set_defaults(fn=cmd_history)
    r = sub.add_parser("report"); r.add_argument("--months", type=int, default=3); r.set_defaults(fn=cmd_report)
    sub.add_parser("real").set_defaults(fn=cmd_real)
    sub.add_parser("schedule").set_defaults(fn=cmd_schedule)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("reparse").set_defaults(fn=cmd_reparse)
    args = p.parse_args(argv)

    conn = store.connect()
    if args.cmd in ("status", "reparse"):
        return args.fn(args, conn, None, args.sn)
    client, preferred = load_client_from_env()
    try:
        return args.fn(args, conn, client, args.sn or preferred)
    except FoxApiError as e:
        print(f"API error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
