# Shipping Module (MarineTraffic Vessel Tracker)

Scrapes vessel particulars, AIS positions and voyages from MarineTraffic's JSON endpoints into PostgreSQL. It runs next to chem-os on the same server and shares chem-os's RDS database:

- **It writes** `mainshipdata` plus its own history tables. chem-os's `/api/v1/ships/*` endpoints read `mainshipdata`.
- **It reads** chem-os's `shipwithid` table. Every ship flagged `my_ship` is added to the watch list and scraped automatically.

It runs on a schedule, twice a day, and exits after each run.

**Deploying, scheduling and operations:** see [DEPLOYMENT.md](DEPLOYMENT.md).

## How it runs

There is no always-running process. A systemd timer starts one scrape run twice a day; the run scrapes every ship once and exits. Between runs, nothing is running.

| When | What happens |
|---|---|
| A random moment between 09:00 and 09:30 IST | Morning run: shows where ships got to overnight. |
| A random moment between 17:00 and 17:30 IST | Evening run. |
| The server was off at run time | The run happens as soon as the server is back up. |

**Each run:**

1. **Syncs the watch list.** It adds every ship chem-os flags `my_ship` in `shipwithid`, and every ship in `mainshipdata`. A ship added to a purchase during the day is picked up by the next run.
2. **Scrapes each ship once,** my-ships first, with a 3–8 second pause between ships. With about 150 ships, a run takes roughly 25–35 minutes. Ships scraped successfully in the last `SKIP_RECENT_HOURS` (2) are skipped, so a manual run followed by a timer run doesn't scrape twice.
3. **Stops early** if MarineTraffic blocks `MAX_CONSECUTIVE_BLOCKS` (3) ships in a row, after `MAX_RUN_MINUTES` (240), or on shutdown. A shutdown first finishes the ship in progress.

**Results:** every attempt is written to `scrape_log`. Each scrape's position is added to `positions`, so the history builds up a route of two points per day. A run that stops early, or in which every ship failed, exits with code 2. systemd then marks it as failed, and it shows up in `systemctl --failed`.

**Load on MarineTraffic:** about 5 requests per ship (4 vessel calls plus port lookups, cached for 24 hours). For about 150 ships that's roughly 1,500 requests a day, one ship at a time.

**Safety:**
- **One run at a time:** a Postgres advisory lock stops a manual run and a timer run from overlapping.
- **Atomic writes:** each ship's history rows and its `mainshipdata` row are written in a single transaction.
- **Isolation on the shared server:** the service runs as its own `shipping` user with no login. It's limited to 400 MB of memory and half a CPU at low priority, the system files are read-only to it, and it can't see home directories.

`ship_id` is MarineTraffic's **shipid**, the number in `marinetraffic.com/.../shipid:314687`. It is not the IMO or MMSI number.

## Deploying

Everything runs on the existing EC2 host (Amazon Linux 2023). There's no Docker. On disk it takes about 160 MB: Python 3.11, plus the current release and one previous release kept for rollback.

**One-time setup:**

1. **Database:** a DBA runs [sql/grants.sql](sql/grants.sql) as an admin user on RDS. It creates the least-privilege `shipping_scraper` user. If the scraper tables already exist, also run section 5.
2. **Server:** run `sudo bash scripts/ec2-setup.sh`. It installs `python3.11` alongside the system Python and creates the `shipping` user, `/opt/shipping-module` and `/etc/shipping-module`. It doesn't touch chem-os, the email platform or the Next.js apps.
3. **GitHub secrets:** add the secrets listed at the top of `.github/workflows/deploy.yml`.

**Each deploy** happens on a push to `main`, or manually from the Actions tab. CI runs the tests, then `scripts/deploy.sh` runs on the server:

1. Unpacks the code into `/opt/shipping-module/releases/<timestamp>` and builds its virtualenv.
2. Writes the settings to `/etc/shipping-module/shipping.env` (readable only by root and `shipping`).
3. Runs `migrate`, which also checks that the database is reachable.
4. Switches `/opt/shipping-module/current` to the new release.
5. Installs the systemd timer and the `shipping` command.
6. Prints `status` and the time of the next run.

A run already in progress finishes on the old release.

**Rollback:** point the `current` link back at the previous release folder. For example:

```
sudo ln -sfn /opt/shipping-module/releases/<previous> /opt/shipping-module/current
```

**On the server:**

```
shipping status                                # read-only: DB check + last 24h of scrapes
sudo systemctl start shipping-scrape.service   # run a full scrape now
shipping run 314687                            # scrape one ship now
systemctl list-timers shipping-scrape.timer    # when the next run is
journalctl -u shipping-scrape -n 100           # logs of recent runs
journalctl -u shipping-scrape -f               # follow a run live
```

**After the first deploy,** check that MarineTraffic doesn't block the server's IP with `shipping run 314687`. If you see `blocked`, set the `SHIPPING_SCRAPER_PROXY` secret and redeploy.

## Local development

```
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt     # Windows
docker run -d --rm --name shipping-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=shipping_test -p 55432:5432 postgres:17
.venv/Scripts/python -m pytest
```

## CLI

```
python main.py migrate                  # create / upgrade the schema
python main.py run                      # sync ships and scrape each once (what the timer runs)
python main.py run 314687 123456        # scrape just these ships
python main.py status                   # DB check + summary of the last 24h of scrapes
python main.py add 314687 123456        # track ships by shipid
python main.py list                     # watch list
python main.py remove 314687            # stop tracking (history kept; the sync won't re-add it)
python main.py show 314687              # latest stored record as JSON (fetches live if unknown)
python main.py track 314687 --geojson   # position history as a route
python main.py export                   # dump all tables to ./export/*.csv
```

## Data sources (per ship, per scrape)

| Endpoint | Gives |
|---|---|
| `/en/vessels/{id}/general` | name, IMO, MMSI, call sign, flag, type, size, market |
| `/en/vessels/{id}/summary` | the two summary sentences |
| `/en/vessels/{id}/position` | lat/lon, speed, course, heading, draught, nav status, area, AIS source |
| `/en/vessels/{id}/voyage` | departure/arrival ports & times, destination, ETA, progress |
| `/en/ports/{id}/general` | port name, UN/LOCODE, country (cached for 24h) |

## Tables

| Table | Contents |
|---|---|
| `ships` | The watch list. `my_ship` mirrors chem-os; my-ships are scraped first. |
| `vessels` | One row per ship, overwritten with the latest particulars. |
| `positions` | Position history, one row per distinct AIS report. |
| `voyages` | One row per voyage or ETA change. Progress is updated in place. |
| `shippingport` | Port lookup. |
| `snapshots` | Raw JSON of every scrape. |
| `scrape_log` | Success or failure of every attempt. |
| `mainshipdata` | Flat, latest-state row per ship. This is the table chem-os reads. |
