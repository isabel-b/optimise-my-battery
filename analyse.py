#!/usr/bin/env python3
"""Analyse stored FoxESS history to see where grid import happens and size an
overnight charge.

Usage:
  python analyse.py                       # all data in the DB
  python analyse.py --cheap 11:00-16:00   # your cheap-rate window (default shown)
  python analyse.py --days 30             # only the last N days
  python analyse.py --capacity 10.4       # override estimated usable battery kWh
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import time as dtime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

from foxess import store
import costs
from tariffs import TARIFFS

OUTPUT = Path(__file__).resolve().parent / "output"

# Colour by entity, fixed across every chart (categorical slots 1..4 + extras).
COL = {
    "import": "#2a78d6", "export": "#eb6834", "pv": "#1baf7a", "load": "#eda100",
    "charge": "#e87ba4", "discharge": "#008300", "soc": "#4a3aa7",
}
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"
SEQ = LinearSegmentedColormap.from_list("seqblue", ["#fcfcfb", "#cde2fb", "#6da7ec", "#256abf", "#0d366b"])
NAMES = {"pv": "Solar", "load": "House load", "import": "Grid import", "export": "Grid export",
         "charge": "Battery charge", "discharge": "Battery discharge"}
POWER_VARS = {"pvPower": "pv", "loadsPower": "load", "gridConsumptionPower": "import",
              "feedinPower": "export", "batChargePower": "charge", "batDischargePower": "discharge"}


# ---- data -------------------------------------------------------------------
def load_history(conn: sqlite3.Connection, sn: str | None = None, days: int | None = None) -> pd.DataFrame:
    q = "SELECT sn, variable, ts, value FROM history"
    params: list = []
    if sn:
        q += " WHERE sn=?"; params.append(sn)
    raw = pd.read_sql_query(q, conn, params=params)
    if raw.empty:
        raise SystemExit("No history in the database yet. Run: python fetch.py history --days 30")
    if not sn and raw["sn"].nunique() > 1:
        sn = raw["sn"].mode()[0]
        raw = raw[raw["sn"] == sn]
    raw["ts"] = pd.to_datetime(raw["ts"])
    df = raw.pivot_table(index="ts", columns="variable", values="value").sort_index()
    df = df.rename(columns=POWER_VARS)
    for col in POWER_VARS.values():
        if col not in df:
            df[col] = 0.0
    if days:
        df = df[df.index >= df.index.max().normalize() - pd.Timedelta(days=days - 1)]
    # Sample interval in hours, per row, so gaps are not counted as energy.
    step = df.index.to_series().diff().dt.total_seconds().div(3600)
    df["dt_h"] = step.clip(upper=0.25).fillna(5 / 60)
    return df


def daily_energy(df: pd.DataFrame) -> pd.DataFrame:
    """kWh per calendar day from the 5-minute power samples. Drops incomplete days."""
    e = df[list(POWER_VARS.values())].multiply(df["dt_h"], axis=0)
    e["samples"] = 1
    daily = e.groupby(e.index.normalize()).sum()
    daily = daily[daily["samples"] >= 250]  # ~87% of a day present
    return daily.drop(columns="samples")


def estimate_capacity(df: pd.DataFrame) -> float | None:
    """Usable kWh, from runs where the battery only charges (or only discharges).

    SoC is reported in whole percent, so per-sample ratios are noisy. Instead find
    consecutive runs where the battery is doing one thing, sum the battery energy
    over the run, and divide by the SoC change. Runs moving SoC by at least 15%
    give a clean ratio; the median across runs is the capacity estimate.
    """
    if "SoC" not in df or df["SoC"].isna().all():
        return None
    net = (df["charge"] - df["discharge"]) * df["dt_h"]
    mode = np.sign(net).replace(0, np.nan).ffill().fillna(0)
    gap = df["dt_h"] > 0.2
    run_id = ((mode != mode.shift()) | gap).cumsum()
    ratios = []
    for _, run in df.assign(net=net, run=run_id).groupby("run"):
        if len(run) < 3:
            continue
        d_soc = (run["SoC"].iloc[-1] - run["SoC"].iloc[0]) / 100.0
        energy = run["net"].sum()
        if abs(d_soc) >= 0.15 and np.sign(d_soc) == np.sign(energy) and energy != 0:
            ratios.append(energy / d_soc)
    if len(ratios) >= 3:
        return float(np.median(ratios))
    if "ResidualEnergy" in df and df["ResidualEnergy"].nunique() > 5:
        m = df[df["SoC"] >= 30]
        return float((m["ResidualEnergy"] / (m["SoC"] / 100)).median())
    return None


def parse_window(spec: str) -> tuple[str, str]:
    a, b = spec.split("-")
    return a.strip(), b.strip()


def in_window(idx: pd.DatetimeIndex, cheap: tuple[str, str]) -> np.ndarray:
    start, end = (dtime.fromisoformat(x) for x in cheap)
    t = idx.time
    if start <= end:
        return np.array([(start <= x < end) for x in t])
    return np.array([(x >= start or x < end) for x in t])  # wraps midnight


def held_back_hours(df: pd.DataFrame, floor: float) -> dict:
    """Hours of the day in which the battery is not discharging even though the house
    has a shortfall and the battery has charge. This is a scheduler/work-mode fingerprint:
    a Backup / Feedin / ForceCharge segment shows up as a block of such hours.
    Returns the hours and the grid import (kWh) that happened inside them."""
    shortfall = (df["load"] > df["pv"] + 0.3) & (df["SoC"] > floor + 5)
    if shortfall.sum() < 50 or "SoC" not in df:
        return {"hours": [], "kwh": 0.0}
    hour = df.index.hour
    n = shortfall.groupby(hour).sum()
    discharging = (shortfall & (df["discharge"] > 0.2)).groupby(hour).sum()
    frac = (discharging / n.replace(0, np.nan)).fillna(1.0)
    hours = [int(h) for h in frac.index if n[h] >= 20 and frac[h] < 0.1]
    kwh = float((df["import"] * df["dt_h"])[np.isin(hour, hours) & (df["SoC"] > floor + 5)].sum())
    return {"hours": hours, "kwh": kwh}


def summarise(df: pd.DataFrame, daily: pd.DataFrame, cheap: tuple[str, str],
              capacity_kwh: float | None) -> dict:
    cheap_mask = in_window(df.index, cheap)
    imp_kwh = df["import"] * df["dt_h"]
    day = df.index.normalize()
    imp_in = imp_kwh[cheap_mask].groupby(day[cheap_mask]).sum().reindex(daily.index, fill_value=0)
    imp_out = imp_kwh[~cheap_mask].groupby(day[~cheap_mask]).sum().reindex(daily.index, fill_value=0)
    soc_min = df["SoC"].groupby(day).min().reindex(daily.index) if "SoC" in df else None
    floor = float(df["SoC"].quantile(0.02)) if "SoC" in df else None  # the min-SoC setting in practice
    held = held_back_hours(df, floor if floor is not None else 10.0)
    s = {
        "held_back_hours": held["hours"],
        "held_back_import_per_day": held["kwh"] / max(len(daily), 1),
        "days": len(daily),
        "period": f"{daily.index.min().date()} to {daily.index.max().date()}",
        "cheap_window": f"{cheap[0]}-{cheap[1]}",
        "capacity_kwh": capacity_kwh,
        "load_mean": daily["load"].mean(),
        "pv_mean": daily["pv"].mean(),
        "import_mean": daily["import"].mean(),
        "export_mean": daily["export"].mean(),
        "import_in_cheap_mean": imp_in.mean(),
        "import_outside_cheap_mean": imp_out.mean(),
        "import_outside_cheap_median": imp_out.median(),
        "import_outside_cheap_p90": imp_out.quantile(0.9),
        "days_with_peak_import": int((imp_out > 0.5).sum()),
        "soc_min_median": None if soc_min is None else soc_min.median(),
        "soc_floor": floor,
        "days_battery_hit_floor": None if soc_min is None else int((soc_min <= floor + 1).sum()),
        "self_sufficiency": 1 - daily["import"].sum() / max(daily["load"].sum(), 1e-9),
    }
    end_t = dtime.fromisoformat(cheap[1])
    at_end = df[(df.index.hour == end_t.hour) & (df.index.minute >= end_t.minute)]
    soc_end = at_end["SoC"].groupby(at_end.index.normalize()).first().reindex(daily.index) if "SoC" in df else None
    if soc_end is not None:
        s["soc_at_window_end_median"] = soc_end.median()
        s["soc_at_window_end_p10"] = soc_end.quantile(0.1)
    if capacity_kwh and soc_end is not None:
        floor_frac = (floor or 10.0) / 100.0
        headroom = (capacity_kwh * (1 - soc_end / 100.0)).fillna(0)
        # Expensive import from window end until the next window start, i.e. what a fuller
        # battery at window end could have covered (capped by the headroom that was available).
        avoidable = np.minimum(imp_out, headroom)
        s["topup_avoidable_per_day"] = avoidable.mean()
        s["topup_avoidable_share"] = avoidable.sum() / max(imp_out.sum(), 1e-9)
        s["topup_needed_median"] = avoidable[avoidable > 0.5].median() if (avoidable > 0.5).any() else 0.0
        s["topup_days"] = int((avoidable > 0.5).sum())
    if capacity_kwh:
        s["shiftable_median"] = min(s["import_outside_cheap_median"], capacity_kwh)
        s["shiftable_p90"] = min(s["import_outside_cheap_p90"], capacity_kwh)
        # Fraction of peak import a full overnight charge could have displaced, day by day.
        covered = np.minimum(imp_out, capacity_kwh).sum() / max(imp_out.sum(), 1e-9)
        s["peak_import_coverable"] = covered
    return s


def print_summary(s: dict) -> None:
    def kwh(x): return "n/a" if x is None else f"{x:5.1f} kWh"
    print(f"Period: {s['period']} ({s['days']} complete days)   off-peak window: {s['cheap_window']}")
    print(f"Estimated usable battery capacity: {kwh(s['capacity_kwh'])}")
    print()
    print(f"Per day (mean)      load {kwh(s['load_mean'])}   solar {kwh(s['pv_mean'])}   "
          f"import {kwh(s['import_mean'])}   export {kwh(s['export_mean'])}")
    print(f"Self-sufficiency    {s['self_sufficiency'] * 100:4.0f}% of load met without the grid")
    print()
    print(f"Grid import inside off-peak   mean {kwh(s['import_in_cheap_mean'])}")
    print(f"Grid import outside off-peak  mean {kwh(s['import_outside_cheap_mean'])}   "
          f"median {kwh(s['import_outside_cheap_median'])}   p90 {kwh(s['import_outside_cheap_p90'])}")
    print(f"Days with >0.5 kWh peak-rate import: {s['days_with_peak_import']} of {s['days']}")
    if s.get("soc_min_median") is not None:
        print(f"Battery daily minimum SoC: median {s['soc_min_median']:.0f}%, "
              f"reached the {s['soc_floor']:.0f}% floor on {s['days_battery_hit_floor']} days")
    if s["held_back_hours"]:
        hrs = s["held_back_hours"]
        print(f"Battery held back (no discharge despite shortfall) during hours {hrs[0]:02d}:00-{hrs[-1] + 1:02d}:00: "
              f"{s['held_back_import_per_day']:.1f} kWh/day imported with charge available")
    if "soc_at_window_end_median" in s:
        print(f"Battery SoC at end of cheap window: median {s['soc_at_window_end_median']:.0f}%, "
              f"p10 {s['soc_at_window_end_p10']:.0f}%")
    if "topup_avoidable_per_day" in s:
        print()
        print("Cheap-window grid top-up (ForceCharge during the cheap window on days the battery will not last):")
        print(f"  would avoid {kwh(s['topup_avoidable_per_day'])} of expensive import per day on average "
              f"({s['topup_avoidable_share'] * 100:.0f}% of it)")
        print(f"  needed on {s['topup_days']} of {s['days']} days; on those days the top-up is typically "
              f"{kwh(s['topup_needed_median'])}")


def print_costs(table: pd.DataFrame, days: int) -> None:
    print()
    print(f"Cost of these {days} days under Ergon tariffs (1 July 2026 rates inc GST, feed-in included), $/day:")
    piv = table.pivot(index="tariff", columns="strategy", values="per_day_$")[["actual", "foresight", "fill"]]
    kwh = table.pivot(index="tariff", columns="strategy", values="import_kwh")
    print(f"  {'tariff':8s} {'actual':>9s} {'foresight':>10s} {'fill':>9s}   saving/day (foresight, fill)   "
          f"import kWh/day (actual -> foresight)")
    for tariff, row in piv.iterrows():
        print(f"  {tariff:8s} {row['actual']:9.2f} {row['foresight']:10.2f} {row['fill']:9.2f}   "
              f"{row['actual'] - row['foresight']:6.2f}  {row['actual'] - row['fill']:6.2f}                 "
              f"{kwh.loc[tariff, 'actual'] / days:5.1f} -> {kwh.loc[tariff, 'foresight'] / days:5.1f}")
    print("  foresight = top up in the cheap window only as much as that night needs (best case for a scheduler);")
    print("  fill = always fill to 100% in the window. Both include 8% charging losses.")


# ---- charts -----------------------------------------------------------------
def _style(ax, title: str, ylabel: str = ""):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c3c2b7")
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold", pad=10)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK2, fontsize=9)


def render_charts(df: pd.DataFrame, daily: pd.DataFrame, out_dir: Path,
                  cheap: tuple[str, str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "sans-serif", "figure.facecolor": SURFACE,
                         "savefig.facecolor": SURFACE, "text.color": INK})
    paths = []

    # 1. Daily energy: four series as grouped thin bars.
    fig, ax = plt.subplots(figsize=(11, 4.2))
    keys = ["load", "pv", "import", "export"]
    x = np.arange(len(daily))
    w = 0.2
    for i, k in enumerate(keys):
        ax.bar(x + (i - 1.5) * w, daily[k], width=w * 0.9, color=COL[k], label=NAMES[k], linewidth=0)
    ax.set_xticks(x[:: max(1, len(x) // 12)])
    ax.set_xticklabels([d.strftime("%d %b") for d in daily.index[:: max(1, len(x) // 12)]])
    _style(ax, "Energy per day", "kWh")
    ax.legend(frameon=False, ncol=4, fontsize=9, loc="upper left", bbox_to_anchor=(0, 1.02))
    fig.tight_layout()
    p = out_dir / "daily_energy.png"; fig.savefig(p, dpi=150); plt.close(fig); paths.append(p)

    # 2. Average day: power profile and SoC on separate panels (one axis each).
    tod = (df.index.hour * 60 + df.index.minute) // 10 * 10
    prof = df.groupby(tod).mean(numeric_only=True)
    hours = prof.index / 60
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.3]})
    for k in ["load", "pv", "import", "export"]:
        ax1.plot(hours, prof[k], color=COL[k], linewidth=2, label=NAMES[k])
        if prof[k].max() > 0.3:
            j = prof[k].idxmax()
            ax1.annotate(NAMES[k], (j / 60, prof[k].loc[j]), textcoords="offset points",
                         xytext=(4, 4), fontsize=8, color=INK2)
    _shade_cheap(ax1, cheap); _shade_cheap(ax2, cheap)
    _style(ax1, "Average day: power by time of day", "kW")
    ax1.legend(frameon=False, ncol=4, fontsize=9, loc="upper left", bbox_to_anchor=(0, 1.02))
    if "SoC" in prof:
        ax2.plot(hours, prof["SoC"], color=COL["soc"], linewidth=2)
        ax2.set_ylim(0, 100)
    _style(ax2, "Battery state of charge", "%")
    ax2.set_xticks(range(0, 25, 3)); ax2.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 3)])
    ax2.set_xlim(0, 24)
    fig.tight_layout()
    p = out_dir / "average_day.png"; fig.savefig(p, dpi=150); plt.close(fig); paths.append(p)

    # 3. Heatmap: grid import kWh per hour, days down the side (sequential single hue).
    imp = (df["import"] * df["dt_h"]).to_frame("kwh")
    imp["day"] = imp.index.normalize(); imp["hour"] = imp.index.hour
    grid = imp.pivot_table(index="day", columns="hour", values="kwh", aggfunc="sum").reindex(columns=range(24), fill_value=0)
    fig, ax = plt.subplots(figsize=(11, max(3.0, 0.22 * len(grid) + 1.5)))
    im = ax.imshow(grid.values, cmap=SEQ, aspect="auto", vmin=0, interpolation="nearest")
    ax.set_xticks(range(0, 24, 3)); ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 3)])
    step = max(1, len(grid) // 15)
    ax.set_yticks(range(0, len(grid), step)); ax.set_yticklabels([d.strftime("%d %b") for d in grid.index[::step]])
    ax.set_facecolor(SURFACE)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.set_title("Grid import by hour (kWh)", loc="left", color=INK, fontsize=11, fontweight="bold", pad=10)
    ax.set_xlabel("Hour of day", color=INK2, fontsize=9)
    cb = fig.colorbar(im, ax=ax, fraction=0.02, pad=0.02); cb.outline.set_visible(False)
    cb.ax.tick_params(colors=MUTED, labelsize=8, length=0)
    fig.tight_layout()
    p = out_dir / "import_heatmap.png"; fig.savefig(p, dpi=150); plt.close(fig); paths.append(p)
    return paths


def _shade_cheap(ax, cheap: tuple[str, str]):
    s, e = (dtime.fromisoformat(x) for x in cheap)
    sh, eh = s.hour + s.minute / 60, e.hour + e.minute / 60
    spans = [(sh, eh)] if sh <= eh else [(sh, 24), (0, eh)]
    for a, b in spans:
        ax.axvspan(a, b, color="#f0efec", zorder=0, linewidth=0)


# ---- main -------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sn")
    p.add_argument("--days", type=int)
    p.add_argument("--cheap", default="11:00-16:00", help="cheap-rate window HH:MM-HH:MM")
    p.add_argument("--capacity", type=float, help="usable battery kWh (else estimated)")
    p.add_argument("--out", default=str(OUTPUT))
    p.add_argument("--feed-in", type=float, help="override feed-in $/kWh")
    p.add_argument("--no-costs", action="store_true", help="skip the tariff cost simulation")
    args = p.parse_args(argv)

    conn = store.connect()
    df = load_history(conn, args.sn, args.days)
    daily = daily_energy(df)
    if daily.empty:
        raise SystemExit("No complete days in the data yet.")
    cap = args.capacity or estimate_capacity(df)
    cheap = parse_window(args.cheap)
    print_summary(summarise(df, daily, cheap, cap))
    if not args.no_costs and cap:
        if args.feed_in is not None:
            for t in TARIFFS.values():
                t.feed_in = args.feed_in
        print_costs(costs.cost_table(df, TARIFFS, cheap, cap, len(daily)), len(daily))
    paths = render_charts(df, daily, Path(args.out), cheap)
    print("\nCharts:")
    for q in paths:
        print(f"  {q}")


if __name__ == "__main__":
    sys.exit(main() or 0)
