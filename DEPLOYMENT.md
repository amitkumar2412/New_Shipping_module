# Shipping Module — Deployment Guide

How the MarineTraffic scraper is deployed and scheduled, why it is built this way, and how to run, change and troubleshoot it.

## At a glance

| | |
|---|---|
| What it does | Scrapes position, voyage and particulars for every tracked ship from MarineTraffic into the chem-os RDS database. |
| Where it runs | The existing chem-os EC2 instance (Amazon Linux 2023), next to chem-os, the email platform and the Next.js apps. |
| How it runs | A systemd timer starts one run twice a day. The run scrapes each ship once and exits. Nothing runs between runs. |
| When | A random moment between **09:00 and 09:30 IST**, and again between **17:00 and 17:30 IST**. |
| Which ships | Every ship chem-os marks as `my_ship` in `shipwithid`, plus every ship in `mainshipdata` (146 at the time of writing). |
| Database | The chem-os database on RDS. It writes `mainshipdata` and its own history tables, and reads `shipwithid`. Currently the **dev** database only. |
| Deployed by | GitHub Actions on every push to `main`, over SSH, using the same approach as chem-os. |
| Footprint | 0 MB of memory and 0 CPU between runs. About 210 MB of disk. |

```
                ┌──────────────── EC2 (Amazon Linux 2023) ────────────────┐
  09:00–09:30   │  systemd timer ──▶ shipping-scrape.service (runs, exits) │
  17:00–17:30   │                     user: shipping, ≤400 MB, ≤½ CPU     │
     IST        └───────────────┬──────────────────────────┬──────────────┘
                                │ HTTPS, one ship at a time │ SQL (TLS)
                                ▼                          ▼
                         MarineTraffic             RDS: chem-os database
                                                   reads  shipwithid (my-ships)
                                                   writes mainshipdata ◀── chem-os reads
                                                          positions, voyages, scrape_log, …
```

---

## 1. Schedule and what each run does

### Timing

| Setting | Value | Where |
|---|---|---|
| Morning run | 09:00 IST plus a random 0–30 minutes | `deploy/shipping-scrape.timer` |
| Evening run | 17:00 IST plus a random 0–30 minutes | `deploy/shipping-scrape.timer` |
| Randomness | A new random delay every day (`RandomizedDelaySec=30min`) | timer |
| Missed run | If the server was off at run time, the run happens once it's back up (`Persistent=true`) | timer |
| Gap between runs | About 8 hours during the day, about 16 hours overnight | |

The timer uses `Asia/Kolkata` explicitly, so it's unaffected by the server's own timezone (UTC). 09:00 IST is 03:30 UTC.

The morning run shows where each ship got to overnight. The evening run shows the day's progress.

### Steps in each run (`python main.py run`)

1. **Take the run lock.** This is a Postgres advisory lock. If another run holds it, for example a manual run, this run logs a message and exits cleanly.
2. **Sync the watch list:**
   - New `my_ship` ships in `shipwithid` are added and marked as my-ships.
   - Ships already tracked that become my-ships (picked for a new purchase) are promoted.
   - Ships no longer flagged in chem-os drop back to normal. They stay tracked.
   - Ships in `mainshipdata` missing from the watch list are added.
   - A ship removed by hand (`main.py remove`) is never re-added.
3. **Choose the ships:** every active ship, **my-ships first**, then the ship waiting longest. A ship scraped successfully in the last `SKIP_RECENT_HOURS` (2) is skipped. That stops a manual run followed by a timer run from scraping it twice.
4. **Scrape one ship at a time.** There's a random 3–8 second pause between ships. Each ship's data is saved in one database transaction.
5. **Stop early** if any of these happens:
   - MarineTraffic blocks `MAX_CONSECUTIVE_BLOCKS` (3) ships in a row.
   - The run reaches `MAX_RUN_MINUTES` (240).
   - systemd stops the service. The ship in progress is finished first.
6. **Exit:**
   - **Code 0** if the run completed. Individual ship failures are normal (for example a wrong ship id) and are recorded in `scrape_log`.
   - **Code 2** if the run stopped early or every ship failed. systemd then marks the run as *failed*.

### What gets stored

| Table | Per run |
|---|---|
| `mainshipdata` | Latest state of each ship, overwritten. This is the table chem-os reads. |
| `positions` | A new row whenever the ship's AIS position has changed. This builds the route over time. |
| `voyages` | A new row when the voyage or ETA changes; otherwise progress is updated. |
| `vessels`, `shippingport` | Latest particulars and port lookup. |
| `snapshots` | Raw JSON of every scrape. |
| `scrape_log` | One row per attempt: success or failure, with the error. |

**Route accuracy:** twice a day gives about two points per ship per day. That's enough to show the general route, progress and ETA. The line between points is straight, and points are roughly 200–400 km apart at sea. For a more detailed track, shorten the schedule; see [Changing the schedule](#changing-the-schedule).

---

## 2. Efficiency

Figures are for 146 ships. Measured values are marked; the rest are estimates.

| Resource | Use | Notes |
|---|---|---|
| Memory between runs | **0** | No process is running. |
| Memory during a run | Hard cap 400 MB (`MemoryMax`) | One Python process; the actual use is expected to be far below the cap. |
| CPU | ≤ 50% of one core, low priority (`Nice=10`, `CPUWeight=20`) | It mostly waits on the network. |
| Run time | About 25–35 minutes, twice a day | About 10–15 seconds per ship, including the polite pause. |
| Disk | About 160 MB (measured: 78 MB per release, 2 kept) plus Python 3.11 (about 50 MB) | Fixed size; old releases are deleted on deploy. Logs go to the system journal, which has a size limit. |
| Requests to MarineTraffic | About 1,500 a day (146 ships × about 5 requests × 2 runs) | One ship at a time. Port lookups are cached for 24 hours. |
| Database load | About 300 small transactions a day | Negligible for RDS. |
| Database growth | Mostly `snapshots`, an estimated 1–2 GB a year | See [Known limitations](#8-known-limitations-and-next-steps). |
| Extra AWS cost | $0 | Uses the existing EC2 instance and RDS database. |

### Compared with the alternatives we considered

| Option | Verdict | Why |
|---|---|---|
| **systemd timer on the existing EC2 (chosen)** | ✅ | Zero use between runs, no new infrastructure. It uses the server's fixed IP and the RDS access that already works, and it's deployed the same way as the other apps. |
| Always-running worker | ❌ | Uses memory all day for a job that runs twice a day. |
| Docker on EC2 | ❌ | Docker and the image need about 1 GB on an 8 GB disk that's already shared, and Docker itself uses about 100 MB of memory. The server runs nothing else in Docker. |
| AWS Lambda | ❌ for now | Lambda's outgoing IPs are shared AWS addresses, which Cloudflare blocks far more often than a fixed EC2 IP. To reach both RDS and the internet, Lambda needs a NAT Gateway (about $35–45 a month), or RDS has to be opened to the internet. The core code is a plain `run()` function, so moving to Lambda later only needs a small wrapper. |

---

## 3. What's on the server

| Path | Contents | Owner / mode |
|---|---|---|
| `/opt/shipping-module/releases/<timestamp>/` | One folder per deploy: the code plus its own `venv/`. The newest two are kept. | `root:shipping`, read-only to the service |
| `/opt/shipping-module/current` | A link to the release in use | `root` |
| `/etc/shipping-module/shipping.env` | Database settings and secrets, rewritten on every deploy | `root:shipping`, mode `640` |
| `/etc/systemd/system/shipping-scrape.service` | The job: runs one scrape and exits | |
| `/etc/systemd/system/shipping-scrape.timer` | The schedule | |
| `/usr/local/bin/shipping` | A helper that runs the CLI as the `shipping` user with the production settings | |

**The `shipping` service user** has no login shell and no home directory. It can read the release and its settings file and nothing else. The service also runs with these restrictions:
- **Read-only system:** `ProtectSystem=strict`.
- **No home directories:** `ProtectHome=true`.
- **Private temp folder:** `PrivateTmp=true`.
- **No privilege gain:** `NoNewPrivileges=true`.

It can't touch chem-os, the email platform or the Next.js apps.

---

## 4. One-time setup

### 4.1 Database (RDS, run by an admin)

Run [sql/grants.sql](sql/grants.sql) against the target database, which is the **dev** database for now. Replace `CHANGE_ME` with a strong password. Avoid spaces, quotes, backslashes and `#`, because the settings file is plain `KEY=value`.

The script:
- creates the `pg_trgm` extension
- creates the `shipping_scraper` user
- gives it read access to `shipwithid`

**The scraper tables already exist, so run section 5 and section 4 together, in that order, in the same session:**
- **Section 5** gives `shipping_scraper` ownership of the scraper tables, so `migrate` can alter them.
- **Section 4** grants chem-os's database user `SELECT` on `mainshipdata`. If chem-os's user owned those tables before, it loses access the moment section 5 runs. **chem-os's ship screens break until section 4 runs.**

### 4.2 Server (once, as `ec2-user`)

This installs Python 3.11 alongside the system Python 3.9 and creates the user and folders. It changes nothing for the other apps.

```bash
sudo dnf install -y python3.11
id shipping >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /sbin/nologin shipping
sudo mkdir -p /opt/shipping-module/releases /etc/shipping-module
sudo chown -R root:shipping /opt/shipping-module /etc/shipping-module
sudo chmod 755 /opt/shipping-module /opt/shipping-module/releases && sudo chmod 750 /etc/shipping-module
python3.11 --version
```

(`scripts/ec2-setup.sh` does the same steps.)

### 4.3 GitHub secrets (in this repo: Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `EC2_HOST` | The instance's public IP (an Elastic IP is recommended). Not a URL. |
| `EC2_USER` | `ec2-user` |
| `EC2_SSH_KEY` | The private key used to SSH to the instance |
| `SHIPPING_DB_HOST` | RDS hostname only, with no `/dbname` |
| `SHIPPING_DB_NAME` | The target database (dev for now) |
| `SHIPPING_DB_USER` | `shipping_scraper` |
| `SHIPPING_DB_PASSWORD` | The password from 4.1 |
| `SHIPPING_SCRAPER_PROXY` | Optional. Only needed if MarineTraffic blocks the server's IP. |

---

## 5. Deploying

**Trigger:** push or merge to `main`, or *Actions → Deploy → Run workflow*.

| Step | Where | What |
|---|---|---|
| 1. Test | GitHub Actions (`ci.yml`) | Runs the test suite against Postgres 17, checks the scripts' syntax and the timer's schedule. |
| 2. Package | GitHub Actions | Creates `git archive` of the commit, and writes the settings file from the secrets. |
| 3. Upload | GitHub Actions → EC2 | Copies the package, the settings file and `scripts/deploy.sh` to `/tmp` over SCP. |
| 4. Install | EC2 (`sudo bash deploy.sh`) | Unpacks into a new release folder → builds its `venv` → compiles the Python files → installs the settings (`640`) → runs `migrate`, which also proves the database is reachable → switches `current` → installs the systemd units and the `shipping` helper → enables the timer → deletes all but the 2 newest releases → prints `status` and the next run time. |
| 5. Clean up | Both | Deletes the uploaded files and the local copy of the secrets. |

A deploy takes about 1–2 minutes. A run already in progress finishes on the old release; the next timer run uses the new one. If `migrate` fails, `current` is not switched, so the old release stays live.

### Rollback

Point `current` back at the previous release:

```bash
ls -1t /opt/shipping-module/releases/            # newest first
sudo ln -sfn /opt/shipping-module/releases/<previous> /opt/shipping-module/current
```

The previous release still works only if no database change it can't handle was made since. Migrations here only add things, so this is normally the case.

---

## 6. Day-to-day operations

```bash
shipping status                                # read-only: DB check + last 24h summary
shipping status --hours 72                     # longer window
systemctl list-timers shipping-scrape.timer    # when the next run is
sudo systemctl start shipping-scrape.service   # run a full scrape now (blocks until done)
shipping run 314687 459221                     # scrape specific ships now
shipping list                                  # watch list
journalctl -u shipping-scrape -n 200           # logs of recent runs
journalctl -u shipping-scrape -f               # follow a run live
systemctl --failed                             # shows shipping-scrape.service if the last run failed
```

A healthy morning run looks like this in the journal:

```
INFO tracker.run Added 1 new my-ship(s) from shipwithid: [314687]
INFO tracker.run Run starting: 146 ship(s) to scrape
INFO tracker.run 314687 MY TANKER | Underway using Engine | lat=… lon=… spd=12.8 | NEW position | 9.4s
…
INFO tracker.run Run finished: 145/146 ok, 31.2 min
WARNING tracker.run Failed ships: [123456]
```

### Changing the schedule

Edit `OnCalendar=` in `deploy/shipping-scrape.timer` and push to `main`. Examples:

```ini
OnCalendar=*-*-* 09:00:00 Asia/Kolkata          # once a day
OnCalendar=*-*-* 00/3:00:00 Asia/Kolkata        # every 3 hours (finer route)
```

### Changing settings

The settings file is rewritten from GitHub secrets on every deploy. Optional settings such as `SKIP_RECENT_HOURS`, `MAX_RUN_MINUTES`, `MAX_CONSECUTIVE_BLOCKS` and `SCRAPER_TIMEOUT` use the defaults in `config.py`. To change one permanently, add a `printf` line for it in the *Build release and settings* step of `.github/workflows/deploy.yml`. An edit to `/etc/shipping-module/shipping.env` on the server only lasts until the next deploy.

---

## 7. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `blocked … on all 3 attempts` and the run stops early | MarineTraffic or Cloudflare is blocking the server's IP | Wait for the next run. If it keeps happening, set `SHIPPING_SCRAPER_PROXY` and redeploy. |
| `Cannot reach the database` | RDS security group, wrong host, or wrong password | Test with `timeout 5 bash -c '</dev/tcp/<DB_HOST>/5432' && echo ok`; check the secrets. |
| Deploy fails at `migrate` with a permission error | Section 5 of `grants.sql` hasn't been run | See [4.1](#41-database-rds-run-by-an-admin). |
| chem-os ship screens empty after the DB setup | Section 4 of `grants.sql` hasn't been run | Grant chem-os's user `SELECT` on `mainshipdata`. |
| `my-ships: table shipwithid NOT FOUND` in `status` | Wrong database, or chem-os hasn't created the table | Check `SHIPPING_DB_NAME`. |
| No runs happening | Timer not enabled | `systemctl list-timers shipping-scrape.timer`; `sudo systemctl enable --now shipping-scrape.timer`. |
| `Another run is already in progress` | A manual run overlapped the timer | Normal. The second run skips itself. |
| Same ship fails every run with `404` | Wrong ship id (must be MarineTraffic's **shipid**, not the IMO or MMSI number) | Fix the id in chem-os, or `shipping remove <id>`. |

---

## 8. Known limitations and next steps

| Item | Impact | Suggested fix |
|---|---|---|
| **No alerting** | A failed run is only visible in `systemctl --failed` and the journal. | Add an `OnFailure=` unit that sends an email through the existing email platform, or a daily check of `shipping status`. |
| **`snapshots` grows forever** | An estimated 1–2 GB a year in RDS. | A monthly job that deletes snapshots older than 90 days. |
| **Dev database only** | Prod chem-os is on an older build without the ship features. | When prod gets them, add a second deployment for the prod database. Keep it to one scraper per ship list, so MarineTraffic isn't hit twice. |
| **Coarse routes** | Two points per day. | Shorten the timer's schedule if detailed tracks are needed. |
| **Unofficial endpoints** | MarineTraffic can block the scraper or change these endpoints at any time, and their terms of service don't allow scraping. | Repeated blocks stop the run cleanly. For critical use, consider MarineTraffic's official API. |
| **Untested against the live site from EC2** | Blocking can't be ruled out until the first real run. | After the first deploy, run `shipping run 314687`. |

---

## 9. What changed from the original module

- **Deployment:** the original was a Windows Task Scheduler or `--loop` script. It's now a systemd timer with a hardened, resource-capped service, deployed by CI to release folders with rollback.
- **Database:**
  - The original used one shared connection, which broke after any error. It now uses a connection pool, and each ship is saved in one transaction.
  - Tables were created on every start; that's now an explicit `migrate` step.
  - The original pointed at a hardcoded, malformed host. Settings are now read from the environment and validated at startup, failing immediately with a clear message.
- **chem-os integration:** my-ships are synced from `shipwithid` and scraped first. Every `mainshipdata` ship is tracked.
- **Scraper:**
  - It now recognises when it's being blocked and stops the run instead of hammering MarineTraffic.
  - Proxy support, configurable timeouts, a bounded 24-hour port cache.
  - Empty or invalid responses are treated as errors.
- **Safety:** a run lock (no overlapping runs), a maximum run time, clean shutdown, and a separate least-privilege database user (`sql/grants.sql`).
- **Removed:** the FastAPI read API (chem-os already serves the same data from the database), Docker, the always-running worker, and the committed sample CSV. A live database password had been committed to `.env.example`. It was removed, but **it remains in git history and must be rotated.**
- **Quality:**
  - Pinned dependencies.
  - 41 tests against a real Postgres 17.
  - The full setup and deploy was rehearsed on Amazon Linux 2023 with systemd 252: setup, deploy, redeploy with release cleanup, the timer's IST schedule, and a run through systemd. That rehearsal used a dead proxy, so it made no live MarineTraffic requests.
