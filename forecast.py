"""Solar forecast from Open-Meteo (free, no key), calibrated against our own PV history.

Open-Meteo gives hourly shortwave radiation (GHI, W/m2). Summing it over a period and
multiplying by a site factor k (kWh of PV per kWh/m2 of GHI), fitted on the last 60 days
of our own data, gives a PV energy forecast. The fit is done on the 11:00-16:00 window
because that is the period the planner cares about.
"""
from __future__ import annotations

import os
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd
import requests

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def site() -> dict:
    """Location from .env / environment: LAT, LON, TZ."""
    from dotenv import load_dotenv
    load_dotenv()
    try:
        return {"lat": float(os.environ["LAT"]), "lon": float(os.environ["LON"]),
                "tz": os.environ.get("TZ", "Australia/Brisbane")}
    except KeyError:
        raise SystemExit("LAT and LON are not set. Add them to .env (see .env.example).")


def fetch_hourly(lat: float, lon: float, tz: str, past_days: int = 60, forecast_days: int = 2) -> pd.DataFrame:
    """Hourly frame indexed by local time: ghi (kWh/m2 for that hour), temp (C), cloud (%)."""
    r = requests.get(OPEN_METEO, params={
        "latitude": lat, "longitude": lon, "timezone": tz,
        "hourly": "shortwave_radiation,temperature_2m,cloud_cover",
        "past_days": past_days, "forecast_days": forecast_days,
    }, timeout=30)
    r.raise_for_status()
    j = r.json()["hourly"]
    # Plain arrays, not Series: a Series would be aligned on its integer index and come out NaN.
    h = pd.DataFrame({"ghi": np.array(j["shortwave_radiation"], dtype=float) / 1000.0,
                      "temp": np.array(j["temperature_2m"], dtype=float),
                      "cloud": np.array(j["cloud_cover"], dtype=float)},
                     index=pd.to_datetime(j["time"]))
    return h


def mod(index: pd.DatetimeIndex) -> pd.Index:
    """Minute of day as integers. Comparing `.time` arrays with datetime.time is unreliable."""
    return index.hour * 60 + index.minute


def mins(t: dtime) -> int:
    return t.hour * 60 + t.minute


def _window_sum(h: pd.DataFrame, start: dtime, end: dtime) -> pd.Series:
    m = (mod(h.index) >= mins(start)) & (mod(h.index) < mins(end))
    return h.loc[m, "ghi"].resample("D").sum()


def calibrate(df_history: pd.DataFrame, hourly: pd.DataFrame, cheap: tuple[str, str]) -> dict:
    """Fit k = PV kWh per kWh/m2 on the cheap window, using complete past days only."""
    s, e = (dtime.fromisoformat(x) for x in cheap)
    win = df_history[(mod(df_history.index) >= mins(s)) & (mod(df_history.index) < mins(e))]
    pv = (win["pv"] * win["dt_h"]).groupby(win.index.normalize()).sum()
    n = win.groupby(win.index.normalize()).size()
    pv = pv[n >= 50]  # a full window is 60 samples
    ghi = _window_sum(hourly, s, e)
    m = pd.DataFrame({"pv": pv, "ghi": ghi}).dropna()
    m = m[m.index < pd.Timestamp.now().normalize()]
    if len(m) < 7 or (m.ghi ** 2).sum() == 0:
        return {"k": 7.0, "days": len(m), "corr": None, "abs_err_median": None}
    k = float((m.pv * m.ghi).sum() / (m.ghi ** 2).sum())
    err = (k * m.ghi - m.pv).abs()
    return {"k": k, "days": int(len(m)), "corr": float(m.pv.corr(m.ghi)),
            "abs_err_median": float(err.median()), "abs_err_p90": float(err.quantile(0.9))}


def ghi_between(hourly: pd.DataFrame, start: datetime, end: datetime) -> float:
    """kWh/m2 between two local datetimes, pro-rating the partial first hour."""
    total = 0.0
    for ts, g in hourly["ghi"].items():
        hs, he = ts, ts + pd.Timedelta(hours=1)
        lo, hi = max(hs, pd.Timestamp(start)), min(he, pd.Timestamp(end))
        if hi > lo:
            total += float(g) * (hi - lo).total_seconds() / 3600.0
    return total
