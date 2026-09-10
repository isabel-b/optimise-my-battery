# optimise-my-battery

Pull data from a FoxESS inverter/battery via the FoxCloud Open API, keep it in a
local SQLite file, and analyse usage to decide when to charge from the grid. Built
for a home in Queensland on an Ergon time-of-use tariff, where grid
power is cheapest 11:00-16:00 and summer evenings mean a lot of air conditioning.
The write side (setting the work-mode schedule) is stubbed in `foxess/client.py`
and will be wired up once the analysis says what schedule makes sense.

## Setup

1. In FoxCloud (web), click your profile icon -> **API Management** -> generate an API key.
2. Put the key in a file called `.api` in this folder, or `cp .env.example .env` and set `FOX_API_KEY`. Both are git-ignored.
3. `source .venv/bin/activate` (already created; `pip install -r requirements.txt` if missing).

## Fetch

```
python fetch.py devices             # confirm the inverter is visible
python fetch.py history --days 60   # 5-minute samples, 1 API call per day
python fetch.py report --months 6   # daily kWh totals from FoxCloud's own reports
python fetch.py status
python fetch.py schedule           # current work-mode time segments (read only)
```

Quota is 1,440 API calls per inverter per day. Fetches are cached in the database,
so re-running only fetches missing days (today and yesterday are always refreshed).
Every raw response is kept in `raw_responses`; `python fetch.py reparse` rebuilds the
parsed tables from that cache without touching the API.

## Analyse

```
python analyse.py --cheap 11:00-16:00
```

Prints a summary and writes charts to `output/`:

- `daily_energy.png`: import, export, solar and load per day
- `average_day.png`: average power profile by time of day with battery SoC
- `import_heatmap.png`: grid import by hour and day, which shows where the money goes

`--cheap` is your cheap-rate tariff window (default 11:00-16:00). The summary reports how much energy you
import outside that window per day, and how much of it a grid top-up inside the window
could have avoided given the battery headroom at the end of the window.

## Dashboard

```
python web.py          # then open http://127.0.0.1:5050
```

A single page over the same database: stat tiles, energy per day, the average-day
profile with the cheap window shaded, a grid-import heatmap, the findings, the tariff
cost table and the inverter's current schedule. Range buttons switch between 7, 14, 30,
60 days or everything. It makes no API calls on its own; the three header buttons do
("Fetch new days" is one call per missing day, "Live now" and "Re-read schedule" one
or two each).

## Tariff costs

`analyse.py` also prices the recorded period under Ergon's residential tariffs (11, 12D,
12E Solar Soaker, 12F Solar Sharer; rates from 1 July 2026 inc GST, regional feed-in
$0.06006/kWh) and simulates two cheap-window strategies:

- `foresight`: top up from the grid in the window only as much as that night needs
  (the best a forecast-driven scheduler can do)
- `fill`: always fill the battery to 100% in the window

Rates live in `tariffs.py`; the simulation is in `costs.py`. Use `--feed-in` to override
the export rate and `--no-costs` to skip this section.

## Caveat on seasonality

The first data covers July to September. In the wet season the evening air-conditioning
load will be much higher and solar can be interrupted by cloud for days, so re-run the
analysis on summer data before settling on a schedule. The daytime free/cheap window
strategy ("Fill" in the cost table) is likely to look much better in summer than it does here.

## Next step: writing the schedule

`FoxClient.scheduler_set` in `foxess/client.py` targets the endpoint the inverter accepts
(`/op/v1/device/scheduler/enable`) but has not been run against the real device yet.
The plan is a daily job around 10:45 that reads SoC and a solar forecast, decides how much
top-up is needed for the coming night, and sets a ForceCharge segment inside 11:00-16:00
(or leaves the existing Backup segment alone on days that do not need it).

## Layout

- `foxess/client.py`: signed requests, endpoints, rate limiting
- `foxess/store.py`: SQLite schema, raw cache, parsers
- `fetch.py`: CLI to pull data
- `analyse.py`: pandas analysis and matplotlib charts
- `tariffs.py`: Ergon tariff definitions and the cost engine
- `costs.py`: cheap-window top-up simulation
- `tests/`: offline tests using synthetic API-shaped data (`pytest`)
