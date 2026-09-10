"""Offline tests: signature format, response parsing, and the ingest -> analyse path
using a synthetic day of data shaped like the real API response."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from foxess import store
from foxess.client import FoxClient, HISTORY_VARIABLES


def fake_day(day: datetime, pv_peak: float = 3.5) -> list[dict]:
    """One day of 5-minute samples with a plausible solar/battery shape."""
    series = {v: [] for v in HISTORY_VARIABLES}
    soc, cap = 20.0, 10.0
    for i in range(288):
        t = day + timedelta(minutes=5 * i)
        h = t.hour + t.minute / 60
        pv = max(0.0, pv_peak * (1 - ((h - 13) / 5) ** 2)) if 8 <= h <= 18 else 0.0
        load = 0.4 + (1.5 if 17 <= h <= 21 else 0.0) + (0.8 if 7 <= h <= 8 else 0.0)
        surplus = pv - load
        charge = discharge = imp = exp = 0.0
        if surplus > 0:
            charge = min(surplus, 3.0) if soc < 100 else 0.0
            exp = surplus - charge
        else:
            discharge = min(-surplus, 3.0) if soc > 10 else 0.0
            imp = -surplus - discharge
        soc = max(10.0, min(100.0, soc + (charge - discharge) * (5 / 60) / cap * 100))
        ts = t.strftime("%Y-%m-%d %H:%M:%S") + " GMT+0100"
        for var, val in [("pvPower", pv), ("loadsPower", load), ("gridConsumptionPower", imp),
                         ("feedinPower", exp), ("batChargePower", charge),
                         ("batDischargePower", discharge), ("SoC", soc),
                         ("ResidualEnergy", soc / 100 * cap)]:
            series[var].append({"time": ts, "value": round(val, 3)})
    return [{"variable": v, "unit": "%" if v == "SoC" else ("kWh" if v == "ResidualEnergy" else "kW"),
             "name": v, "data": pts} for v, pts in series.items()]


def test_signature_matches_documented_formula(monkeypatch):
    c = FoxClient(api_key="abc")
    monkeypatch.setattr("foxess.client.time.time", lambda: 1700000000.123)
    h = c._headers("/op/v0/device/list")
    assert h["timestamp"] == "1700000000123"
    expected = hashlib.md5(b"/op/v0/device/list\r\nabc\r\n1700000000123").hexdigest()
    assert h["signature"] == expected


def test_normalise_time_variants():
    assert store.normalise_time("2024-01-17 12:05:00 GMT+0000") == "2024-01-17 12:05:00"
    assert store.normalise_time("2024-01-17T12:05:00Z") == "2024-01-17 12:05:00"
    with pytest.raises(ValueError):
        store.normalise_time("nonsense")


def test_parse_report_both_shapes():
    a = {"data": [{"variable": "loads", "unit": "kWh",
                   "data": [{"time": "2024-03-01 00:00:00", "value": 1.5}, {"time": "2024-03-02 00:00:00", "value": None}]}]}
    b = [{"variable": "loads", "unit": "kWh", "values": [1.5, None, 2.0]}]
    assert store.parse_report(a, "month", 2024, 3, 1) == [("2024-03-01 00:00:00", "loads", 1.5, "kWh")]
    assert store.parse_report(b, "month", 2024, 3, 1) == [
        ("2024-03-01 00:00:00", "loads", 1.5, "kWh"), ("2024-03-03 00:00:00", "loads", 2.0, "kWh")]
    assert store.parse_report(b, "day", 2024, 3, 5)[1][0] == "2024-03-05 02:00:00"


def test_ingest_and_analyse(tmp_path: Path):
    conn = store.connect(tmp_path / "t.sqlite")
    sn = "TEST123"
    store.upsert_devices(conn, [{"deviceSN": sn, "deviceType": "H1-5.0-E", "hasBattery": True, "hasPV": True}])
    day0 = datetime(2026, 8, 1)
    for i in range(7):
        datas = fake_day(day0 + timedelta(days=i))
        store.cache(conn, "history", sn, (day0 + timedelta(days=i)).date().isoformat(), datas)
        assert store.insert_history(conn, sn, datas) == 288 * len(HISTORY_VARIABLES)
    assert len(store.history_days_present(conn, sn)) == 7

    import analyse
    df = analyse.load_history(conn, sn)
    assert df.shape[0] == 7 * 288
    daily = analyse.daily_energy(df)
    assert len(daily) == 7
    # energy balance: load == pv - export + import + discharge - charge (within rounding)
    bal = daily["pv"] - daily["export"] + daily["import"] + daily["discharge"] - daily["charge"]
    assert (abs(bal - daily["load"]) < 0.05).all()
    cap = analyse.estimate_capacity(df)
    assert 9.5 <= cap <= 10.5

    summary = analyse.summarise(df, daily, cheap=("23:30", "05:30"), capacity_kwh=cap)
    assert "import_outside_cheap_median" in summary
    out = analyse.render_charts(df, daily, tmp_path, cheap=("23:30", "05:30"))
    assert all(p.exists() and p.stat().st_size > 1000 for p in out)


def test_topup_simulation_moves_import_into_cheap_window(tmp_path: Path):
    from datetime import datetime, timedelta
    import costs
    from tariffs import TARIFFS
    conn = store.connect(tmp_path / "c.sqlite")
    sn = "T"
    for i in range(5):
        store.insert_history(conn, sn, fake_day(datetime(2026, 8, 1) + timedelta(days=i), pv_peak=1.2))
    import analyse
    df = analyse.load_history(conn, sn)
    cheap = ("11:00", "16:00")
    base = costs.simulate(df, cheap, 10.0, "actual")
    fore = costs.simulate(df, cheap, 10.0, "foresight")
    win = costs._window_mask(df.index, cheap)
    e = lambda d, m: float((d["import"] * d["dt_h"])[m].sum())
    # expensive import goes down, cheap-window import goes up by at most the losses-adjusted amount
    assert e(fore, ~win) < e(base, ~win)
    assert e(fore, win) > e(base, win)
    assert e(fore, win) - e(base, win) <= (e(base, ~win) - e(fore, ~win)) / costs.ROUND_TRIP_EFF + 1e-6
    table = costs.cost_table(df, TARIFFS, cheap, 10.0, 5)
    assert set(table["strategy"]) == {"actual", "foresight", "fill"}
    row = table.set_index(["tariff", "strategy"])["total_$"]
    assert row[("12E", "foresight")] < row[("12E", "actual")]
