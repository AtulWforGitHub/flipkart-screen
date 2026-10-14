# Flipkart Price Tracker - System Management

## Production Runbook: Hostinger

This section describes current Linux production operations. The Windows laptop
instructions later in this document are legacy, not the Hostinger procedure.
Last verified: 2026-10-14. Never store passwords, MongoDB connection strings,
Telegram tokens, or session-cookie values in this guide.

### Quick Reference

| Setting | Value |
| --- | --- |
| Branch | `main` (direct commits; no feature branches for this project) |
| SSH destination | `root@187.127.191.228` |
| App directory | `/root/flipkart-app` |
| systemd unit | `flipkart` |
| HTTP base URL | `http://187.127.191.228:8000` |
| Activity file | `/root/flipkart-app/activity.log` |
| Database | MongoDB Atlas, database `flipkart` |

The repository is public. Inspect staged changes before committing and obtain
owner approval before pushing. Enter passwords directly at terminal prompts,
never in command arguments or chat. Prefer SSH keys for repeated access. Verify
an unfamiliar host-key fingerprint through a trusted channel before accepting it.

### Architecture and File Ownership

- `app.py`: FastAPI routes, MongoDB state, background scraping, alerts, logs,
	health checks, hourly restart, and worker orchestration.
- `flipkart_scraper.py`: Reusable Selenium Chrome session, cookies, price reads,
	DOM NEP preference, hard-timeout watchdog, and timing measurements.
- `bank_offers_extractor.py`: Carousel navigation and fallback offer parsing.
- `config.py`: Browser settings, worker count, offers, screenshots, and test mode.
- `dashboard.html`: Main web dashboard; admin pages are generated in `app.py`.
- `cookie_manager.py` and `flipkart_login.py`: Cookie persistence and login.
- `requirements.txt`: Python dependencies; does not install Chrome itself.
- `CONTEXT.md`: Additional implementation notes and historical gotchas.
- `client-package/`: Separate client login tooling; not part of the eight-file
	server deployment.

### Deployment Checklist

1. Confirm the intended branch/revision and preserve unrelated local edits.
2. Run local checks and a representative live scrape when scraper behavior changes.
3. Commit only intended files; inspect the staged diff before an approved push.
4. Copy the eight production files. Stop on transfer failure.
5. Restart systemd only after all files copy successfully.
6. Confirm fresh startup, HTTP availability, and successful post-restart scrapes.
7. Check a complete cycle and offer coverage when performance logic changes.

### Deploy From macOS

Run from the repository root:

```bash
git status --short --branch
git switch main
git pull --ff-only
git log -1 --oneline
```

Deployment copies working-tree files, not an immutable Git archive. Confirm the
eight files match the release you intend to deploy. Do not silently discard
uncommitted changes. Copy exactly this list:

```bash
scp app.py dashboard.html flipkart_scraper.py flipkart_login.py \
		config.py cookie_manager.py bank_offers_extractor.py requirements.txt \
		root@187.127.191.228:/root/flipkart-app/
```

Do NOT deploy `compare_offer_coverage.py`, `compare_cycle_offer_logs.py`, or
`hostinger_cycle_monitor.py`: these are local analysis tools. Do not use
`scripts/deploy.ps1` on macOS; it contains a stale Windows path.

After the copy succeeds:

```bash
ssh root@187.127.191.228 "systemctl restart flipkart && systemctl is-active flipkart"
```

Expected output: `active`. This is a service-state check, not proof that app
initialization or scraping has completed. Verify the endpoints below next.
SCP is not an atomic release: a failed copy can leave partially updated files.
Do not restart after a failed transfer or blindly retry a failed SSH command.

Copying `requirements.txt` does not install dependencies. For dependency changes,
first inspect `systemctl cat flipkart` to identify the actual Python environment,
then install into that environment before restarting. The server's interpreter,
virtualenv, Chrome version, and complete unit configuration are not recorded here
because they have not been inspected. Do not guess those paths.
Dependencies in `requirements.txt` are currently unpinned; a fresh installation
can resolve different versions. Record versions before upgrading production.

### Verify Startup and Progress

```bash
curl --fail --show-error --silent --max-time 30 \
		"http://187.127.191.228:8000/hello"
curl --fail --show-error --silent --max-time 30 \
		"http://187.127.191.228:8000/activity-logs?limit=30"
curl --fail --show-error --silent --max-time 30 \
		"http://187.127.191.228:8000/products"
```

Look for a NEW `Application started` line and no subsequent MongoDB authentication
errors. Then look for `/products` entries with `status: "OK"` and
`last_success_ts` newer than the restart. Baseline values are loaded from MongoDB
at startup, so merely seeing prices is not proof of a new scrape. Historical
cycle-completion logs are also not proof of post-deployment progress.

`last_success_ts` uses UTC. Activity timestamps use the process's local timezone;
compare with server time rather than assuming the zones match:

```bash
ssh root@187.127.191.228 "date; date -u; systemctl show flipkart -p ActiveEnterTimestamp"
```

A full cycle historically takes about 25-33 minutes for 90 products; optimizations
may change this. Do not require immediate `Cycle #1 completed` output. The app
requests an hourly restart, resetting cycle numbers and discarding in-flight work.
Completed database writes remain persisted. Confirm systemd's restart policy with
`systemctl cat flipkart`; process exit alone cannot guarantee a restart.

### Logs and Diagnostics

| Interface | Purpose |
| --- | --- |
| `/` | Product dashboard |
| `/admin` | Uptime, browser, login, restart controls, activity/live logs |
| `/admin/logs` | Live console log page |
| `/activity-logs?limit=100` | Recent on-disk activity lines as JSON |
| `/admin/logs/stream` | SSE console-log stream |
| `/products` | Current in-memory product snapshots |
| `/alerts` | In-memory recent alerts; resets with the process |
| `/api/history/PID?days=2` | Persisted price history for a tracked PID |
| `/stream` | SSE product updates |

```bash
ssh root@187.127.191.228 "systemctl status flipkart --no-pager"
ssh root@187.127.191.228 "systemctl cat flipkart"
ssh root@187.127.191.228 "journalctl -u flipkart -n 100 --no-pager"
ssh root@187.127.191.228 "tail -n 100 /root/flipkart-app/activity.log"
ssh root@187.127.191.228 "journalctl -u flipkart -f"
```

Ctrl-C stops following the journal, not the app. Per-product prices and offers are
console-only: use the journal/live logs, not just `activity.log`. Activity logs
rotate at 10 MB with five backups. In-memory console capture retains 200 lines.
If SSE times out, check `/products`, the activity endpoint, and the journal before
concluding the scraper is down. A `Scraper: Running` health line means a scraper
object exists; fresh successful timestamps are stronger evidence of progress.

The new `Timing summary` activity line appears after a completed cycle and reports
average, median, maximum, slowest PIDs, and page/price versus offer times. Product
wall time includes retries and excludes the intended gap approximately. Detailed
price/offer times describe the successful scraper attempt and are not refreshed
for the app-level empty-offer retry. They are diagnostic, not exact profiling.

### Local Setup and Tests

Use Python 3.10 or newer (the source uses union type annotations), Chrome/Chromium,
and the project's requirements. A local virtualenv avoids changing global Python:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m py_compile app.py flipkart_scraper.py bank_offers_extractor.py
python3 -m unittest test_compare_cycle_offer_logs -v
```

Check `.gitignore` before creating local artifacts; do not commit virtualenvs,
cookies, screenshots, debug HTML, or logs. If you create `temp/` inside the repo,
ensure `temp/` is ignored.

For a live scrape, replace `ACTUAL_PID` with a current product PID:

```bash
python3 flipkart_scraper.py ACTUAL_PID --headless --delay 8
```

The CLI writes `scrape_results.json`, including raw HTML. Keep it private and out
of Git. For seller-specific validation, use `FlipkartScraper.scrape(pid, lid)` in
a short Python check with the product's actual LID; the CLI does not accept LID.
Driver resolution prefers configured/local/system drivers, then Selenium Manager.
`GOOGLE_CHROME_BIN`, `CHROME_BIN`, and `CHROMEDRIVER_PATH` can select installed
binaries. A compatible browser/driver pair and network access are required.

Starting the complete app is NOT an isolated test. Importing `app.py` initializes
MongoDB/logging; application startup performs database operations and launches
scraping and Telegram polling against configured production services. Avoid
running a second production bot unintentionally. Only in an intentionally isolated
environment, a local server can be started with:

```bash
python3 -m uvicorn app:app --host 127.0.0.1 --port 8001
```

### Configuration and Scraper Behavior

Current checked-in defaults: browser scraping, one worker, headless Chrome,
0.5-second render wait, bank offers enabled, alert screenshots enabled, and test
mode disabled. Treat `config.py` as the source of truth if these change.

- `SCRAPER_WORKERS > 1` creates a browser per worker. Validate memory, rate limits,
	offer coverage, and alert behavior before increasing production concurrency.
- `TEST_MODE` filters scraping to `TEST_PIDS`; it does not isolate MongoDB or
	Telegram, and must not be accidentally deployed enabled.
- The current app retries when live bank offers are empty and then skips the
	product if offers remain empty. Do not assume disabling bank offers will keep
	normal price persistence working without examining this behavior.
- DOM "Lowest price for you" takes precedence over embedded JSON NEP when found.
- Cashback Best Value offers can trigger an NEP adjustment using the first later
	non-cashback offer; compare raw and adjusted values when investigating alerts.
- The browser is reused. Scrape attempts have a default 55-second hard watchdog;
	retries, teardown, and initialization can extend total caller time.
- Faster carousel stopping depends on container text changing. Visual movement
	without a text change can stop extraction early; check real offer coverage.
- Linux/macOS timeout cleanup kills direct driver children and the driver, not a
	guaranteed complete descendant process tree.

### Cookies and Login

Scraper cookie priority: MongoDB account `primary`, then `FLIPKART_COOKIES`
environment JSON, then the first existing local file among `client_cookies.json`,
`vasu_cookies.json`, `flipkart_cookies.json`, then hardcoded fallback cookies.
Fallback cookies can be expired; successful cookie loading is not proof of login.
Browser/account UI checks may also be heuristic.

Run interactive login locally, enter OTP/login details in the browser, and follow
the terminal prompts:

```bash
python3 flipkart_login.py
```

The root login script saves to the constant account key `primary` and also writes
a local cookie backup. Confirm the MongoDB upload succeeded; a backup alone does
not update an already running server. Inspect the tool
before running upload operations: `upload_server_cookies.py` saves to account
`server`, which the main scraper does NOT select. It is not a substitute for
refreshing `primary`.

Existing browsers retain their loaded sessions. After an intentional cookie
update, restart the service or explicitly reset browsers:

```bash
curl --fail --show-error --silent --max-time 30 -X POST \
		"http://187.127.191.228:8000/reload-browser"
```

This interrupts existing browser work; do not run it merely to inspect status.
Never publish cookie JSON or page HTML containing session data.

### Database and Alerts

The deliberately configured MongoDB identity is `jagsir`; do not reproduce its
credential here or casually change the application's credential convention.

| Collection | Purpose |
| --- | --- |
| `products` | PID, name, URL, LID, target, last target-alert NEP |
| `price_history` | Price records and UTC `ts` / `ts_last` |
| `tg_subscribers` | Telegram subscription recipients |
| `cookies` | Named sessions and update time |
| `flipkart_accounts` | Account-related records; inspect tooling before changing |

Startup creates indexes on `(pid, ts DESC)` in history and unique `pid` and
`chat_id` indexes in products/subscribers. Same-price scrapes update `ts_last`
instead of inserting another row. Startup seeds prices from the latest non-null
FSP and NEP independently. New products require LID for seller-specific tracking.

Targets alert when NEP is at/below target and differs from the saved target-alert
NEP; rising above target resets that marker. Without a target, price changes can
alert in either direction. An NEP change of at most four rupees from the preceding
value suppresses an alert. The configured owner also receives alerts independently
of the subscriber collection. Telegram failures do not prove scraping has failed.

Deleting a product also deletes its price history. Never use DELETE endpoints as
a diagnostic. `/test-telegram` sends real messages, `/debug-scrape/PID` launches
scraping and writes HTML, and `/api/screenshot/PID` launches a separate browser.
These GET routes have side effects and are not passive health checks.

### Coverage Analysis (Local Only)

Capture representative per-product console logs before and after changes. For
production use the journal, not activity-only logs. Treat captures as private.

```bash
python3 compare_offer_coverage.py captured_cycle.log --limit 12
python3 compare_cycle_offer_logs.py before.log after.log --limit 20
python3 hostinger_cycle_monitor.py --help
python3 hostinger_cycle_monitor.py state
python3 hostinger_cycle_monitor.py capture-latest --local-output captured_cycle.log
```

Inspect each tool's help and source before longer captures. The cycle monitor uses
SSH/SCP and needs working port 22 access. Some earlier assistant environments
could not reach SSH; this session's deployment succeeded. Do not treat an old
sandbox timeout as proof the server is unreachable today. Cycle numbers reset
on restarts; compare timestamps and startup boundaries as well as cycle numbers.
`compare_offer_coverage.py` resolves product metadata from MongoDB and performs
fresh browser scrapes against the baseline log; it is NOT an offline-only check.
`compare_cycle_offer_logs.py` compares two existing logs offline. Offer availability
can change between captures, so missing offers are evidence to investigate rather
than automatic proof of a regression.

### Troubleshooting

| Symptom | Next check |
| --- | --- |
| SSH banner timeout | Network/port 22/firewall access; stop the deploy and report exact error |
| SCP partial transfer | Verify all eight files before restart; do not claim a successful release |
| Unit failed/inactive | `systemctl status`, journal, unit interpreter and working directory |
| Active but HTTP unavailable | Startup journal, Mongo connectivity, bind/port, firewall |
| Mongo auth errors | Credential deployed consistently, Atlas identity/roles/network access |
| Prices visible but no new scrapes | Fresh `last_success_ts`, console errors, browser/cookies/offers |
| No live offers after retry | Actual offer availability and carousel completeness for that PID/LID |
| Chrome/driver error | Browser and driver versions, binary paths, memory, process cleanup |
| Unexpected NEP | Raw DOM value, cashback adjustment, offer identity and seller LID |
| Telegram connection error | Telegram/network status; inspect scrape progress independently |
| SSE request timeout | Activity JSON, product timestamps and SSH journal |

Do not expose credentials while collecting diagnostics. No new authentication
layer was added by this release: inspect access controls before exposing admin
routes publicly. Prefer firewall restrictions or an authenticated TLS reverse
proxy; plain HTTP endpoints should not be treated as secure admin access.

An old privileged Atlas account was removed from code in `60f425f`, but the repo
notes say its credential remains in public history and its Atlas identity may
still exist. Verify deployment of the replacement identity before an authorized
owner deletes/revokes the old one in Atlas. This deployment did not delete accounts
or rotate credentials. Also rotate any server password shared in chat.

### Rollback

Identify a known-good revision with `git log`, inspect its compatibility, and
coordinate rollback before making changes. Do not reset or force-push `main`.
Use `git revert` for an approved code rollback, then test, commit/push the revert,
and deploy the same eight files. A revert restores source behavior, not database
history, cookies, external credentials, or installed dependency versions.

Do NOT roll back the Mongo identity migration to a revoked/exposed credential.
Rollback only the intended change, not unrelated fixes. Retain private diagnostic
logs and verify fresh successful scrapes after restarting the rolled-back release.

### Release Verification: 2026-10-14

Code commit `7e0bc2a` replaced the three supplied Python files byte-for-byte.
Compilation, three existing offer-log tests, 30 isolated mocked behavior checks,
and a live tracked-product scrape passed. The live scrape returned FSP 79,990,
NEP 68,240, three offers, and both timing metrics. An earlier example PID returned
no prices; the tracked-product/LID check was used for successful live validation.
The 30 mocked checks were inline session checks, not a committed test suite.

All eight files copied successfully. The service returned `active`, and startup
was logged at `2026-10-14 18:25:48 UTC` (`23:55:48` process-local time). Subsequent
HTTP verification found two successful scrapes newer than restart and Telegram
deliveries. The live SSE verification request timed out; independent product data
confirmed resumed scraping. A full post-release cycle and complete cross-product
offer-coverage comparison were not observed in this session.

---

## Legacy Windows Laptop Operations

## Auto-Restart Scheduled Task

A scheduled task named `FlipkartAppRestart` runs every 2 hours to automatically restart the app.

### Check Task Status
```powershell
Get-ScheduledTask -TaskName "FlipkartAppRestart" | Select-Object TaskName, State
```

### Disable Auto-Restart (Stop automatic restarts)
```powershell
Disable-ScheduledTask -TaskName "FlipkartAppRestart"
```

### Enable Auto-Restart (Start automatic restarts)
```powershell
Enable-ScheduledTask -TaskName "FlipkartAppRestart"
```

### Remove Auto-Restart Completely
```powershell
Unregister-ScheduledTask -TaskName "FlipkartAppRestart" -Confirm:$false
```

### View Next Run Time
```powershell
Get-ScheduledTaskInfo -TaskName "FlipkartAppRestart" | Select-Object NextRunTime, LastRunTime, LastTaskResult
```

---

## SSH Remote Access

### Connection Details
- **IP Address**: Check with `.\remote-commands.ps1`
- **Username**: Vasu
- **Port**: 22

### Remote Restart Command
```bash
ssh Vasu@<laptop-ip> "cd Desktop\Freelancer\flipkart-screen; & '.\start-app-ngrok.ps1'"
```

### Check SSH Service Status
```powershell
Get-Service -Name sshd | Select-Object Name, Status, StartType
```

### Stop SSH Service
```powershell
Stop-Service -Name sshd
```

### Start SSH Service
```powershell
Start-Service -Name sshd
```

---

## Manual App Management

### Start App Manually
```powershell
& "c:\Users\Vasu\Desktop\Freelancer\flipkart-screen\start-app-ngrok.ps1"
```

### Kill Port 8000 (if app is stuck)
```powershell
Get-NetTCPConnection -LocalPort 8000 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### Check Running Python Processes
```powershell
Get-Process -Name python -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, StartTime
```

---

## Flipkart Login Cookies

### Update Cookies (login with new credentials)
```powershell
python flipkart_login.py
```

### Delete Current Cookies
```powershell
Remove-Item flipkart_cookies.json -Force
```

---

## Quick Reference

**Most Common Commands:**

1. **Disable auto-restart**: `Disable-ScheduledTask -TaskName "FlipkartAppRestart"`
2. **Enable auto-restart**: `Enable-ScheduledTask -TaskName "FlipkartAppRestart"`
3. **Manual restart**: `& "c:\Users\Vasu\Desktop\Freelancer\flipkart-screen\start-app-ngrok.ps1"`
4. **Kill stuck app**: `Get-NetTCPConnection -LocalPort 8000 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }`

---

## Files Location
All scripts are in: `c:\Users\Vasu\Desktop\Freelancer\flipkart-screen\`

- `start-app-ngrok.ps1` - Main startup script
- `setup-ssh.ps1` - SSH server installation (requires admin)
- `setup-auto-restart.ps1` - Auto-restart task setup (requires admin)
- `remote-commands.ps1` - Show remote access commands
- `flipkart_login.py` - Update Flipkart cookies

---

## Asking Copilot for Help

**Example prompts:**
- "Disable the auto-restart task"
- "Check when the app will restart next"
- "Stop SSH service"
- "Kill the app running on port 8000"
- "Update Flipkart login cookies"
- "Show me how to restart the app manually"
