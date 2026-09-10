"""Minimal client for the FoxESS Cloud Open API.

Auth: every request carries a `token` header (your API key), a millisecond
`timestamp`, and a `signature` = md5(path + "\\r\\n" + token + "\\r\\n" + timestamp).

Limits (per the official docs): 1,440 calls per inverter per day, query endpoints
at most once per second, write endpoints at most once every 2 seconds.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests

BASE_URL = "https://www.foxesscloud.com"

# 5-minute power/state samples available from the history endpoint.
HISTORY_VARIABLES = [
    "pvPower",               # kW  solar generation
    "loadsPower",            # kW  house consumption
    "gridConsumptionPower",  # kW  import from grid
    "feedinPower",           # kW  export to grid
    "batChargePower",        # kW  into battery
    "batDischargePower",     # kW  out of battery
    "SoC",                   # %   battery state of charge
    "ResidualEnergy",        # kWh energy remaining in battery
]

# Daily/hourly energy totals from the report endpoint. Note the odd casing of
# the battery variables: that is what the API actually expects.
REPORT_VARIABLES = [
    "generation",
    "feedin",
    "loads",
    "gridConsumption",
    "chargeEnergyToTal",
    "dischargeEnergyToTal",
]


class FoxApiError(RuntimeError):
    def __init__(self, errno: int, msg: str, path: str):
        super().__init__(f"{path} -> errno {errno}: {msg}")
        self.errno = errno
        self.msg = msg
        self.path = path


ERRNO_HINTS = {
    40256: "request header missing (bad signature/timestamp?)",
    40257: "request body parameters invalid",
    40400: "rate limit hit",
    41808: "device offline",
    41809: "token invalid",
    41930: "no devices on account",
    40401: "daily quota exhausted",
}


@dataclass
class FoxClient:
    api_key: str
    base_url: str = BASE_URL
    timeout: float = 55.0
    session: requests.Session = field(default_factory=requests.Session)
    calls_made: int = 0
    _last_call: dict[str, float] = field(default_factory=dict)

    # ---- transport -------------------------------------------------------
    def _headers(self, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = hashlib.md5(f"{path}\r\n{self.api_key}\r\n{ts}".encode("utf-8")).hexdigest()
        return {
            "token": self.api_key,
            "timestamp": ts,
            "signature": sig,
            "lang": "en",
            "Content-Type": "application/json",
            "User-Agent": "optimise-my-battery/0.1",
        }

    def _throttle(self, path: str, min_gap: float) -> None:
        last = self._last_call.get(path)
        if last is not None:
            wait = min_gap - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[path] = time.monotonic()

    def _request(self, method: str, path: str, body: dict | None = None,
                 params: dict | None = None, min_gap: float = 1.1) -> Any:
        """Send one signed request and return the `result` field.

        Retries once on a rate-limit error after a pause. Raises FoxApiError on
        any non-zero errno so callers never silently store garbage.
        """
        for attempt in range(3):
            self._throttle(path, min_gap)
            resp = self.session.request(
                method, self.base_url + path, headers=self._headers(path),
                json=body, params=params, timeout=self.timeout,
            )
            self.calls_made += 1
            resp.raise_for_status()
            payload = resp.json()
            errno = payload.get("errno", -1)
            if errno == 0:
                return payload.get("result")
            if errno == 40400 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            hint = ERRNO_HINTS.get(errno, payload.get("msg", "unknown error"))
            raise FoxApiError(errno, hint, path)
        raise FoxApiError(40400, ERRNO_HINTS[40400], path)

    # ---- endpoints -------------------------------------------------------
    def device_list(self) -> list[dict]:
        result = self._request("POST", "/op/v0/device/list",
                               {"currentPage": 1, "pageSize": 10})
        return result.get("data", []) if isinstance(result, dict) else []

    def device_detail(self, sn: str) -> dict:
        return self._request("GET", "/op/v1/device/detail", params={"sn": sn})

    def variables(self) -> dict:
        return self._request("GET", "/op/v0/device/variable/get")

    def real_time(self, sn: str, variables: Iterable[str] | None = None) -> list[dict]:
        body: dict[str, Any] = {"sn": sn}
        if variables:
            body["variables"] = list(variables)
        result = self._request("POST", "/op/v0/device/real/query", body)
        if isinstance(result, list) and result:
            return result[0].get("datas", [])
        return []

    def history_day(self, sn: str, day: datetime,
                    variables: Iterable[str] = HISTORY_VARIABLES) -> list[dict]:
        """5-minute samples for one local calendar day (the API caps a call at 24h)."""
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        body = {
            "sn": sn,
            "variables": list(variables),
            "begin": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
        }
        result = self._request("POST", "/op/v0/device/history/query", body)
        if isinstance(result, list) and result:
            return result[0].get("datas", [])
        return []

    def report(self, sn: str, dimension: str, year: int, month: int = 1, day: int = 1,
               variables: Iterable[str] = REPORT_VARIABLES) -> Any:
        """Energy totals. dimension='day' -> 24 hourly values, 'month' -> daily, 'year' -> monthly.

        Returned raw because the response shape has varied between API revisions;
        see store.parse_report for the tolerant parser.
        """
        body = {
            "sn": sn, "dimension": dimension, "variables": list(variables),
            "year": year, "month": month, "day": day,
        }
        return self._request("POST", "/op/v0/device/report/query", body)

    # ---- scheduler (write side, to be wired up in a later step) ----------
    # Paths per the Open API doc: v3 is the current time-segment API.
    def scheduler_flag(self, sn: str) -> Any:
        """Whether the scheduler main switch is on."""
        return self._request("POST", "/op/v0/device/scheduler/get/flag", {"deviceSN": sn})

    def scheduler_get(self, sn: str) -> Any:
        """Current time segments plus the ranges/enums this inverter accepts.
        Verified working: POST /op/v1/device/scheduler/get (the v3 path in the docs 404s)."""
        return self._request("POST", "/op/v1/device/scheduler/get", {"deviceSN": sn})

    def scheduler_set(self, sn: str, groups: list[dict]) -> Any:
        """Write time segments. Untested against the real device. Each group looks like:
            {"enable": 1, "startHour": 2, "startMinute": 0, "endHour": 5, "endMinute": 0,
             "workMode": "ForceCharge", "minSocOnGrid": 10, "fdSoc": 10, "fdPwr": 0, "maxSoc": 100}
        workMode: SelfUse | Feedin | Backup | ForceCharge | ForceDischarge.
        Not exercised yet: keep reads and analysis first, then enable writes deliberately.
        """
        return self._request("POST", "/op/v1/device/scheduler/enable",
                             {"deviceSN": sn, "groups": groups}, min_gap=2.2)


def load_client_from_env() -> tuple[FoxClient, str | None]:
    """Build a client from .env / environment. Returns (client, preferred_sn_or_None)."""
    import os
    from dotenv import load_dotenv

    load_dotenv()
    key = os.environ.get("FOX_API_KEY", "").strip()
    if not key:
        # Fallback: a bare key in a file called .api next to this project (git-ignored).
        api_file = Path(__file__).resolve().parent.parent / ".api"
        if api_file.exists():
            key = api_file.read_text().strip().splitlines()[0].strip()
    if not key:
        raise SystemExit("No API key found. Put it in .env as FOX_API_KEY=... or in a file called .api")
    return FoxClient(api_key=key), (os.environ.get("FOX_DEVICE_SN") or "").strip() or None
