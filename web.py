#!/usr/bin/env python3
"""Local dashboard: python web.py  ->  http://127.0.0.1:5050

Reads data/foxess.sqlite. The only API calls it makes are on demand:
  "Fetch new days" pulls any missing days plus today (1 call per day),
  "Live" reads the inverter's real-time values (1 call).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, jsonify, render_template, request

import analyse
import costs
from foxess import store
from foxess.client import FoxApiError, load_client_from_env
from tariffs import TARIFFS

app = Flask(__name__)
CHEAP = ("11:00", "16:00")
_lock = threading.Lock()
_cache: dict = {}


def _db_mtime() -> float:
    return store.DB_PATH.stat().st_mtime if store.DB_PATH.exists() else 0.0


def _clean(o):
    """Replace NaN/inf with None recursively; browsers reject NaN in JSON."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


def _series(s: pd.Series) -> list:
    return [None if (isinstance(v, float) and np.isnan(v)) else (round(float(v), 3)) for v in s]


def build_overview(days: int | None) -> dict:
    key = (days, _db_mtime())
    with _lock:
        if key in _cache:
            return _cache[key]
    conn = store.connect()
    df = analyse.load_history(conn, None, days)
    daily = analyse.daily_energy(df)
    cap = analyse.estimate_capacity(df)
    summary = analyse.summarise(df, daily, CHEAP, cap)
    summary = {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in summary.items()}

    tod = (df.index.hour * 60 + df.index.minute) // 10 * 10
    prof = df.groupby(tod).mean(numeric_only=True)
    imp = (df["import"] * df["dt_h"]).to_frame("kwh")
    imp["day"] = imp.index.normalize(); imp["hour"] = imp.index.hour
    heat = imp.pivot_table(index="day", columns="hour", values="kwh", aggfunc="sum").reindex(columns=range(24), fill_value=0)

    cost_rows = []
    if cap:
        table = costs.cost_table(df, TARIFFS, CHEAP, cap, len(daily))
        cost_rows = table.round(3).to_dict(orient="records")

    sched = None
    row = conn.execute("SELECT payload FROM raw_responses WHERE endpoint='scheduler' AND key='latest'").fetchone()
    if row:
        sched = json.loads(row[0])
    last = df.iloc[-1]
    out = {
        "days": days, "summary": summary, "capacity": cap, "cheap": CHEAP,
        "daily": {"dates": [d.strftime("%Y-%m-%d") for d in daily.index],
                  **{k: _series(daily[k]) for k in ["load", "pv", "import", "export", "charge", "discharge"]}},
        "profile": {"minutes": [int(m) for m in prof.index],
                    **{k: _series(prof[k]) for k in ["load", "pv", "import", "export", "SoC"] if k in prof}},
        "heatmap": {"dates": [d.strftime("%d %b") for d in heat.index], "hours": list(range(24)),
                    "values": [[round(float(v), 2) for v in r] for r in heat.values]},
        "costs": cost_rows,
        "schedule": sched,
        "last_sample": {"ts": df.index[-1].strftime("%Y-%m-%d %H:%M"), "SoC": float(last.get("SoC", np.nan)),
                        "pv": float(last["pv"]), "load": float(last["load"]),
                        "import": float(last["import"]), "export": float(last["export"])},
    }
    out = _clean(out)
    with _lock:
        _cache.clear(); _cache[key] = out
    return out


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json")


@app.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole app.
    resp = app.send_static_file("sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/overview")
def api_overview():
    days = request.args.get("days", type=int)
    try:
        return jsonify(build_overview(days if days and days > 0 else None))
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/refresh")
def api_refresh():
    """Fetch missing days (and today) via fetch.py in a subprocess so quota use is explicit."""
    days = request.json.get("days", 3) if request.is_json else 3
    proc = subprocess.run([sys.executable, "fetch.py", "history", "--days", str(days)],
                          capture_output=True, text=True, cwd=Path(__file__).parent, timeout=600)
    _cache.clear()
    return jsonify({"ok": proc.returncode == 0, "log": (proc.stdout + proc.stderr)[-2000:]})


@app.get("/api/live")
def api_live():
    try:
        client, preferred = load_client_from_env()
        conn = store.connect()
        sn = preferred or conn.execute("SELECT sn FROM devices LIMIT 1").fetchone()[0]
        vals = {v["variable"]: v.get("value") for v in client.real_time(sn)}
        keep = ["pvPower", "loadsPower", "gridConsumptionPower", "feedinPower", "batChargePower",
                "batDischargePower", "SoC", "batTemperature", "invTemperation", "runningState"]
        return jsonify({"ts": datetime.now().strftime("%H:%M:%S"), **{k: vals.get(k) for k in keep}})
    except (FoxApiError, SystemExit, Exception) as e:  # surfaced to the page, never crashes the server
        return jsonify({"error": str(e)}), 502


@app.get("/api/plan")
def api_plan():
    conn = store.connect()
    conn.executescript("CREATE TABLE IF NOT EXISTS decisions (ts TEXT PRIMARY KEY, day TEXT NOT NULL, mode TEXT NOT NULL, "
                       "max_soc INTEGER, deficit_kwh REAL, inputs TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0, result TEXT);")
    rows = []
    for ts, mode, max_soc, deficit, inputs, applied, result in conn.execute(
            "SELECT ts, mode, max_soc, deficit_kwh, inputs, applied, result FROM decisions ORDER BY ts DESC LIMIT 14"):
        i = json.loads(inputs)
        rows.append({"ts": ts, "mode": mode, "max_soc": max_soc, "deficit_kwh": deficit, "applied": bool(applied),
                     "soc_now": i.get("soc_now"), "solar_window_used": i.get("solar_window_used"),
                     "need_p75": i.get("need_p75"), "expected_soc_end": i.get("expected_soc_end"),
                     "target_soc_end": i.get("target_soc_end"), "morning_factor": i.get("morning_factor"),
                     "result": json.loads(result) if result else None})
    armed = (Path(__file__).parent / ".apply-schedule").exists()
    return jsonify({"armed": armed, "decisions": rows})


@app.post("/api/plan/run")
def api_plan_run():
    """Dry run of the planner on demand (one live SoC read plus the weather fetch)."""
    proc = subprocess.run([sys.executable, "planner.py"], capture_output=True, text=True,
                          cwd=Path(__file__).parent, timeout=180)
    return jsonify({"ok": proc.returncode == 0, "log": (proc.stdout + proc.stderr)[-3000:]})


@app.post("/api/schedule/refresh")
def api_schedule_refresh():
    proc = subprocess.run([sys.executable, "fetch.py", "schedule"], capture_output=True, text=True,
                          cwd=Path(__file__).parent, timeout=120)
    _cache.clear()
    return jsonify({"ok": proc.returncode == 0, "log": (proc.stdout + proc.stderr)[-2000:]})


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")   # set HOST=0.0.0.0 to serve the LAN/Tailscale
    port = int(os.environ.get("PORT", "5050"))
    print(f"Dashboard: http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
