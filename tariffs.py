"""Ergon Energy residential tariffs (regional Queensland), rates from 1 July 2026, inc GST.
Source: ergon.com.au residential tariffs page, read 2026-09-10.

Each tariff is a list of (start "HH:MM", end "HH:MM", $/kWh) windows covering the day,
plus a daily supply charge. 12F's free window has a 24 kWh/day cap handled specially.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime

import numpy as np
import pandas as pd


@dataclass
class Tariff:
    name: str
    supply_per_day: float
    windows: list[tuple[str, str, float]]           # (start, end, $/kWh); end exclusive; may wrap midnight
    free_window: tuple[str, str, float, float] | None = None  # (start, end, cap_kwh, rate_after_cap)
    feed_in: float = 0.0                              # $/kWh paid for export

    def rate_series(self, idx: pd.DatetimeIndex) -> pd.Series:
        """$/kWh for each timestamp (ignoring the 12F free cap, applied per day in cost())."""
        t = idx.time
        out = np.full(len(idx), np.nan)
        for start, end, rate in self.windows:
            s, e = dtime.fromisoformat(start), dtime.fromisoformat(end)
            if s < e:
                m = np.array([(s <= x < e) for x in t])
            else:
                m = np.array([(x >= s or x < e) for x in t])
            out[m & np.isnan(out)] = rate
        return pd.Series(out, index=idx)

    def cost(self, import_kwh: pd.Series, export_kwh: pd.Series | None = None) -> pd.DataFrame:
        """Daily cost in $ from per-sample import kWh (and optional export kWh)."""
        rate = self.rate_series(import_kwh.index)
        charge = import_kwh * rate
        if self.free_window:
            fs, fe, cap, after = self.free_window
            s, e = dtime.fromisoformat(fs), dtime.fromisoformat(fe)
            in_free = pd.Series([(s <= x < e) for x in import_kwh.index.time], index=import_kwh.index)
            # Energy in the free window is free up to `cap` per day, then `after` per kWh.
            per_day_cum = import_kwh.where(in_free, 0).groupby(import_kwh.index.normalize()).cumsum()
            over = (per_day_cum - cap).clip(lower=0)
            over_inc = over.groupby(import_kwh.index.normalize()).diff().fillna(over)
            charge = charge.where(~in_free, over_inc * after)
        day = import_kwh.index.normalize()
        df = pd.DataFrame({"usage": charge.groupby(day).sum()})
        df["supply"] = self.supply_per_day
        df["export_credit"] = 0.0 if export_kwh is None else -(export_kwh * self.feed_in).groupby(day).sum()
        df["total"] = df["usage"] + df["supply"] + df["export_credit"]
        return df


FEED_IN = 0.06006  # regional Qld feed-in from 1 July 2026 ($/kWh, set by the QCA)

TARIFFS = {
    "12E": Tariff("12E Solar Soaker", 1.56730, [
        ("11:00", "16:00", 0.07004),
        ("16:00", "21:00", 0.47207),
        ("21:00", "11:00", 0.25593),
    ]),
    "12F": Tariff("12F Solar Sharer", 1.77853, [
        ("11:00", "14:00", 0.08101),   # replaced by the free window below up to 24 kWh/day
        ("14:00", "16:00", 0.08101),
        ("16:00", "21:00", 0.48902),
        ("21:00", "11:00", 0.26412),
    ], free_window=("11:00", "14:00", 24.0, 0.08101)),
    "12D": Tariff("12D Time of use", 1.56730, [
        ("11:00", "16:00", 0.18512),
        ("16:00", "21:00", 0.40651),
        ("21:00", "11:00", 0.25044),
    ]),
    "11": Tariff("11 Flat rate", 1.80508, [("00:00", "24:00", 0.28895)]),
}
# "24:00" is not a valid time; express the flat tariff as a wrap-around window instead.
TARIFFS["11"].windows = [("00:00", "00:00", 0.28895)]
for _t in TARIFFS.values():
    _t.feed_in = FEED_IN
