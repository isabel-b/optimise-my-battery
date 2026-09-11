#!/usr/bin/env python3
"""Decide each morning whether to force-charge from the grid in the cheap window.

  python planner.py            # dry run: print and record the decision, change nothing
  python planner.py --apply    # also write the schedule to the inverter
  python planner.py --history  # show recent decisions

Logic (all energies in kWh, run just before the cheap window opens):
  1. Where is the battery now (live read), and how big is it.
  2. How much will solar add before the window closes: Open-Meteo GHI x site factor,
     scaled by how this morning's actual PV compared with its own forecast, then
     multiplied by a pessimism factor because under-charging costs ~4x more per kWh
     than over-charging on this tariff.
  3. How much the house will take out of the battery from window close until the next
     window opens: the 75th percentile of the last 14 cycles, counting grid import at
     the floor as unmet need.
  4. Target SoC at window close = floor + need / capacity + margin. If the expected SoC
     falls short, set the cheap-window segment to ForceCharge with maxSoc = target;
     otherwise leave it as Backup.

Decisions are recorded in the `decisions` table and shown on the dashboard.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, time as dtime, timedelta

import numpy as np
import pandas as pd

import analyse
import forecast
from foxess import store
from foxess.client import FoxApiError, load_client_from_env

CHEAP = ("11:00", "16:00")
PESSIMISM = 0.75        # use this fraction of the solar forecast
MARGIN_PCT = 5.0        # extra SoC on top of the computed need
MIN_TOPUP_KWH = 0.5     # ignore deficits smaller than this
NEED_QUANTILE = 0.75
LOOKBACK_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    ts TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    mode TEXT NOT NULL,
    max_soc INTEGER,
    deficit_kwh REAL,
    inputs TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    result TEXT
);
"""


# ---- pure decision -----------------------------------------------------------
def decide(soc_now: float, capacity: float, floor: float, solar_window: float,
           load_window: float, need_night: float, pessimism: float = PESSIMISM,
           margin_pct: float = MARGIN_PCT) -> dict:
    """Return the decision given the inputs. Pure function so it can be tested."""
    solar_eff = solar_window * pessimism
    surplus = max(0.0, solar_eff - load_window)
    headroom = capacity * (1 - soc_now / 100.0)
    soc_end = soc_now + min(surplus, headroom) / capacity * 100.0
    target = min(100.0, floor + need_night / capacity * 100.0 + margin_pct)
    deficit = max(0.0, (target - soc_end) / 100.0 * capacity)
    mode = "ForceCharge" if deficit >= MIN_TOPUP_KWH else "Backup"
    return {
        "mode": mode, "max_soc": int(math.ceil(target)) if mode == "ForceCharge" else 100,
        "deficit_kwh": round(deficit, 2), "expected_soc_end": round(soc_end, 1),
        "target_soc_end": round(target, 1), "solar_window_used": round(solar_eff, 2),
        "surplus_window": round(surplus, 2),
    }


# ---- inputs from data ----------------------------------------------------------
def night_need(df: pd.DataFrame, cheap: tuple[str, str], floor: float, days: int = LOOKBACK_DAYS) -> dict:
    """kWh the battery had to supply from window close to the next window open, per cycle.
    Grid import while at the floor counts as need the battery could not meet."""
    s, e = (dtime.fromisoformat(x) for x in cheap)
    t = forecast.mod(df.index)
    outside = (t >= forecast.mins(e)) | (t < forecast.mins(s))
    x = df[outside]
    cycle = (x.index - pd.Timedelta(hours=e.hour, minutes=e.minute)).normalize()
    net = (x["discharge"] - x["charge"]) * x["dt_h"]
    unmet = (x["import"] * x["dt_h"]).where(x["SoC"] <= floor + 1, 0.0)
    per = pd.DataFrame({"net": net.groupby(cycle).sum(), "unmet": unmet.groupby(cycle).sum(),
                        "n": x.groupby(cycle).size()})
    per = per[per["n"] >= 200]  # complete cycles only (19 h = 228 samples)
    per = per.iloc[-days:]
    need = per["net"] + per["unmet"]
    return {"need_p75": float(need.quantile(NEED_QUANTILE)), "need_median": float(need.median()),
            "need_max": float(need.max()), "cycles": int(len(per))}


def window_load(df: pd.DataFrame, cheap: tuple[str, str], days: int = LOOKBACK_DAYS) -> float:
    s, e = (dtime.fromisoformat(x) for x in cheap)
    t = forecast.mod(df.index)
    w = df[(t >= forecast.mins(s)) & (t < forecast.mins(e))]
    per = (w["load"] * w["dt_h"]).groupby(w.index.normalize()).sum()
    per = per[w.groupby(w.index.normalize()).size() >= 50].iloc[-days:]
    return float(per.median()) if len(per) else 8.0


def morning_factor(df: pd.DataFrame, hourly: pd.DataFrame, k: float, now: datetime) -> float:
    """Actual PV so far today divided by what the forecast implied. Clamped to [0.4, 1.2]."""
    start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    today = df[(df.index >= start) & (df.index <= now)]
    if len(today) < 12:
        return 1.0
    actual = float((today["pv"] * today["dt_h"]).sum())
    predicted = k * forecast.ghi_between(hourly, start, now)
    if predicted < 1.0:
        return 1.0
    return float(min(1.2, max(0.4, actual / predicted)))


def build_inputs(conn, client, sn: str | None, now: datetime, live: bool) -> dict:
    df = analyse.load_history(conn, sn)
    df = df[df.index <= now]  # matters only when --now is used to replay a past morning
    sn = sn or conn.execute("SELECT sn FROM devices LIMIT 1").fetchone()[0]
    capacity = analyse.estimate_capacity(df) or 40.0
    floor = float(df["SoC"].quantile(0.02))
    site = forecast.site()
    hourly = forecast.fetch_hourly(site["lat"], site["lon"], site["tz"])
    cal = forecast.calibrate(df, hourly, CHEAP)
    s, e = (dtime.fromisoformat(x) for x in CHEAP)
    win_start = max(now, now.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0))
    win_end = now.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    ghi_win = forecast.ghi_between(hourly, win_start, win_end) if win_end > win_start else 0.0
    mf = morning_factor(df, hourly, cal["k"], now)
    solar_window = cal["k"] * ghi_win * mf
    soc_source = "last_sample"
    soc_now = float(df["SoC"].iloc[-1]); soc_ts = df.index[-1].strftime("%Y-%m-%d %H:%M")
    if live and client is not None:
        vals = {v["variable"]: v.get("value") for v in client.real_time(sn, ["SoC"])}
        if vals.get("SoC") is not None:
            soc_now, soc_source, soc_ts = float(vals["SoC"]), "live", now.strftime("%Y-%m-%d %H:%M")
    need = night_need(df, CHEAP, floor)
    return {
        "sn": sn, "now": now.strftime("%Y-%m-%d %H:%M"), "soc_now": soc_now, "soc_source": soc_source,
        "soc_ts": soc_ts, "capacity": round(capacity, 1), "floor": floor,
        "ghi_window": round(ghi_win, 3), "k": round(cal["k"], 2), "calibration": cal,
        "morning_factor": round(mf, 2), "solar_window_raw": round(solar_window, 2),
        "load_window": round(window_load(df, CHEAP), 2), **{k_: round(v, 2) if isinstance(v, float) else v for k_, v in need.items()},
        "pessimism": PESSIMISM, "margin_pct": MARGIN_PCT,
    }


# ---- schedule write ------------------------------------------------------------
def apply_decision(client, sn: str, decision: dict, cheap: tuple[str, str]) -> dict:
    """Rewrite the cheap-window segment's mode and maxSoc, leaving everything else as is."""
    current = client.scheduler_get(sn)
    groups = current.get("groups", [])
    s, e = (dtime.fromisoformat(x) for x in cheap)
    idx = next((i for i, g in enumerate(groups) if g.get("enable") and g["startHour"] == s.hour
                and g["startMinute"] == s.minute and g["endHour"] == e.hour and g["endMinute"] == e.minute), None)
    if idx is None:
        raise RuntimeError(f"no enabled segment {cheap[0]}-{cheap[1]} found on the inverter; refusing to write")
    before = dict(groups[idx])
    if before["workMode"] == decision["mode"] and before.get("maxSoc") == decision["max_soc"]:
        return {"changed": False, "before": before}
    keys = ["enable", "startHour", "startMinute", "endHour", "endMinute", "workMode",
            "minSocOnGrid", "fdSoc", "fdPwr", "maxSoc"]
    new_groups = [{k: g.get(k) for k in keys} for g in groups]
    new_groups[idx]["workMode"] = decision["mode"]
    new_groups[idx]["maxSoc"] = decision["max_soc"]
    client.scheduler_set(sn, new_groups)
    after = client.scheduler_get(sn).get("groups", [])[idx]
    ok = after.get("workMode") == decision["mode"] and after.get("maxSoc") == decision["max_soc"]
    return {"changed": True, "before": before, "after": after, "verified": ok}


# ---- main ----------------------------------------------------------------------
def record(conn, now: datetime, decision: dict, inputs: dict, applied: bool, result: dict | None):
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO decisions (ts, day, mode, max_soc, deficit_kwh, inputs, applied, result) VALUES (?,?,?,?,?,?,?,?)",
                 (now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"), decision["mode"], decision["max_soc"],
                  decision["deficit_kwh"], json.dumps({**inputs, **decision}), int(applied), json.dumps(result) if result else None))
    conn.commit()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write the decision to the inverter")
    p.add_argument("--no-live", action="store_true", help="use the last stored SoC instead of a live read")
    p.add_argument("--history", action="store_true", help="print recent decisions and exit")
    p.add_argument("--sn")
    p.add_argument("--now", help="pretend it is this local time, e.g. 2026-09-11T10:45 (dry run only; uses stored data up to then)")
    args = p.parse_args(argv)
    if args.now and args.apply:
        raise SystemExit("--now is for dry runs only")
    conn = store.connect(); conn.executescript(SCHEMA)
    if args.history:
        for row in conn.execute("SELECT ts, mode, max_soc, deficit_kwh, applied, result FROM decisions ORDER BY ts DESC LIMIT 20"):
            print(row)
        return 0
    client = None
    if not args.no_live or args.apply:
        client, preferred = load_client_from_env()
        args.sn = args.sn or preferred
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()
    inputs = build_inputs(conn, client, args.sn, now, live=not args.no_live and not args.now)
    d = decide(inputs["soc_now"], inputs["capacity"], inputs["floor"], inputs["solar_window_raw"],
               inputs["load_window"], inputs["need_p75"])
    print(f"{inputs['now']}  SoC {inputs['soc_now']:.0f}% ({inputs['soc_source']})  capacity {inputs['capacity']} kWh  floor {inputs['floor']:.0f}%")
    print(f"  solar in window: forecast {inputs['solar_window_raw']} kWh (GHI {inputs['ghi_window']} x k {inputs['k']} x morning {inputs['morning_factor']}), "
          f"used {d['solar_window_used']} after pessimism; window load {inputs['load_window']} -> surplus {d['surplus_window']} kWh")
    print(f"  night need p75 {inputs['need_p75']} kWh (median {inputs['need_median']}, max {inputs['need_max']}, {inputs['cycles']} cycles)")
    print(f"  expected SoC at 16:00 {d['expected_soc_end']}%  target {d['target_soc_end']}%  deficit {d['deficit_kwh']} kWh")
    print(f"  DECISION: {d['mode']}" + (f" to {d['max_soc']}%" if d['mode'] == 'ForceCharge' else " (leave as is)"))
    result = None
    if args.apply:
        try:
            result = apply_decision(client, inputs["sn"], d, CHEAP)
            print("  applied:", "no change needed" if not result["changed"] else ("verified" if result.get("verified") else "WRITE NOT VERIFIED"))
        except (FoxApiError, RuntimeError) as e:
            result = {"error": str(e)}; print("  apply failed:", e)
    else:
        print("  dry run: nothing written (use --apply)")
    record(conn, now, d, inputs, args.apply and bool(result and result.get("changed") is not None and "error" not in result), result)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
