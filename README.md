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

## Force-charge planner

`planner.py` runs at 10:45 each day (launchd job `plan`) and decides whether the
11:00-16:00 segment should be ForceCharge or stay as Backup:

1. Live battery SoC and the estimated capacity.
2. Solar expected before 16:00: Open-Meteo radiation (no key; site from `LAT`/`LON`/`TZ`
   in `.env`) times a factor fitted on the last 60 days of our own PV, scaled by how this
   morning's actual PV compared to its forecast, then multiplied by 0.75 because a wrong
   "charge" costs about 7c/kWh and a wrong "don't charge" about 26c/kWh.
3. Tonight's need: 75th percentile of what the battery supplied from 16:00 to 11:00 over
   the last 14 cycles, counting grid import at the floor as unmet need.
4. Target SoC at 16:00 = floor + need / capacity + 5%. If the expected SoC falls short, the
   segment becomes ForceCharge with `maxSoc` = target; otherwise Backup.

It is a **dry run by default**: decisions are recorded in the `decisions` table and shown on
the dashboard, but nothing is written. Creating an empty file `.apply-schedule` in the repo
root on the mini turns writes on; deleting it turns them off. Replay a past morning with
`python planner.py --now 2026-08-30T10:45`.

## Running it on a Mac mini

The intended home is an always-on Mac mini; the laptop is only for development.
`scripts/deploy/` holds the pieces, modelled on the calorie-tracker deployment:

- `start-server.sh`: the dashboard, bound to all interfaces (`HOST=0.0.0.0`) so it is
  reachable on the LAN and over Tailscale
- `fetch-daily.sh`: nightly at 00:30, pulls the last three days of history and two
  months of reports (about five API calls)
- `deploy.sh`: every two minutes, pulls `main` and restarts the server if it changed
- `install.sh`: installs the three as LaunchDaemons (needs `sudo` so they start at boot)

First time on the mini:

```
cd ~ && git clone https://github.com/isabel-b/optimise-my-battery.git
cd optimise-my-battery
/opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# copy .api (and data/foxess.sqlite if you have history already) from the laptop
sudo ./scripts/deploy/install.sh
```

After that, pushing to `main` is the deploy. The repo is public so the mini pulls
over HTTPS without any key.

To install it as an app on a phone it must be served over HTTPS. Tailscale does that
on the tailnet with a real certificate (the calorie-tracker already uses port 443):

```
sudo tailscale serve --bg --https=8443 5050
tailscale serve status     # prints https://<machine>.<tailnet>.ts.net:8443
```

Open that URL in Chrome on the phone and choose "Install app". The page ships a web
manifest, icons and a small service worker, so it opens standalone and keeps the
last good data when the tailnet is unreachable.

**If another PWA is already installed from the same hostname** (here, the calorie
tracker on port 443), Chrome on Android treats every page on that hostname as part of
it, regardless of port, and offers "Open <that app>" instead of an install. The fix is a
second Tailscale node on the mini with its own name:

```
sudo ./scripts/deploy/setup-battery-node.sh    # joins the tailnet as "battery", serves 5050
```

Then the app lives at `https://battery.<tailnet>.ts.net/` and installs normally.

## Layout

- `foxess/client.py`: signed requests, endpoints, rate limiting
- `foxess/store.py`: SQLite schema, raw cache, parsers
- `fetch.py`: CLI to pull data
- `analyse.py`: pandas analysis and matplotlib charts
- `tariffs.py`: Ergon tariff definitions and the cost engine
- `costs.py`: cheap-window top-up simulation
- `tests/`: offline tests using synthetic API-shaped data (`pytest`)
