"""Price the recorded history under Ergon tariffs and simulate cheap-window top-ups.

A "cycle" runs from the start of the cheap window (11:00) to the next day's 11:00: the
window itself is when we can buy cheap energy, and everything after it until the next
window is what the battery has to cover.

Strategies simulated:
  actual    what really happened
  foresight top up exactly enough in the window to cover that cycle's expensive import
            (an upper bound on what a forecast-driven scheduler can achieve)
  fill      always fill the battery to 100% in the window regardless of need
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from tariffs import Tariff

ROUND_TRIP_EFF = 0.92     # kWh out of the battery per kWh bought to charge it
MAX_CHARGE_KW = 10.0      # inverter/battery charge limit (fdPwr max is 10.5 kW)


def _window_mask(idx: pd.DatetimeIndex, cheap: tuple[str, str]) -> np.ndarray:
    s, e = (dtime.fromisoformat(x) for x in cheap)
    t = idx.time
    return np.array([(s <= x < e) for x in t]) if s <= e else np.array([(x >= s or x < e) for x in t])


def simulate(df: pd.DataFrame, cheap: tuple[str, str], capacity: float, strategy: str) -> pd.DataFrame:
    """Return a copy of df with adjusted 'import' and 'export' kWh-rate columns for the strategy."""
    out = df.copy()
    if strategy == "actual":
        return out
    start = dtime.fromisoformat(cheap[0])
    cycle = (df.index - pd.Timedelta(hours=start.hour, minutes=start.minute)).normalize()
    in_win = _window_mask(df.index, cheap)
    imp_kwh = df["import"] * df["dt_h"]
    new_imp = imp_kwh.copy()
    for cid, g in df.groupby(cycle):
        gi = g.index
        win = gi[in_win[df.index.get_indexer(gi)]]
        after = gi[~in_win[df.index.get_indexer(gi)]]
        if len(win) < 12 or len(after) < 12:
            continue
        soc_end = df.loc[win[-1], "SoC"]
        if pd.isna(soc_end):
            continue
        headroom = max(0.0, capacity * (1 - soc_end / 100.0))
        expensive = imp_kwh.loc[after]
        need = float(expensive.sum())
        topup = headroom if strategy == "fill" else min(need, headroom)
        if topup <= 0.05:
            continue
        # Remove the *last* `topup` kWh of expensive import in the cycle: a fuller battery
        # runs out later, so the import it displaces is the latest import, not the earliest.
        remaining = min(topup, need)
        for ts in reversed(after):
            if remaining <= 0:
                break
            take = min(remaining, new_imp.loc[ts])
            new_imp.loc[ts] -= take
            remaining -= take
        # Buy topup/eff in the window, front-loaded from window start at up to MAX_CHARGE_KW.
        to_buy = topup / ROUND_TRIP_EFF
        for ts in win:
            if to_buy <= 0:
                break
            room = max(0.0, MAX_CHARGE_KW - df.loc[ts, "charge"]) * df.loc[ts, "dt_h"]
            add = min(room, to_buy)
            new_imp.loc[ts] += add
            to_buy -= add
    out["import"] = new_imp / df["dt_h"]
    return out


def price(df: pd.DataFrame, tariff: Tariff) -> pd.Series:
    imp = df["import"] * df["dt_h"]
    exp = df["export"] * df["dt_h"]
    c = tariff.cost(imp, exp)
    return c.sum()


def cost_table(df: pd.DataFrame, tariffs: dict[str, Tariff], cheap: tuple[str, str],
               capacity: float, days: int) -> pd.DataFrame:
    rows = []
    sims = {s: simulate(df, cheap, capacity, s) for s in ("actual", "foresight", "fill")}
    for key, t in tariffs.items():
        for s, sdf in sims.items():
            c = price(sdf, t)
            rows.append({"tariff": key, "strategy": s, "usage_$": c["usage"], "supply_$": c["supply"],
                         "export_$": c["export_credit"], "total_$": c["total"],
                         "per_day_$": c["total"] / days, "import_kwh": float((sdf["import"] * sdf["dt_h"]).sum())})
    return pd.DataFrame(rows)
