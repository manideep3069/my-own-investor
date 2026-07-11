# Setup

## 1. Environment

```bash
conda env create -f environment.yaml
conda activate my-own-investor
pip install -e ".[dev]"
pre-commit install
```

## 2. Configuration

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Purpose |
|---|---|---|
| `MOI_EDGAR_IDENTITY` | yes (for 13F/insider data) | SEC-mandated contact, e.g. `Jane Doe jane@example.com` |
| `MOI_FRED_API_KEY` | recommended | macro series — free at fred.stlouisfed.org |
| `MOI_QUIVER_API_KEY` | optional (paid) | congressional trades |
| `MOI_IBKR__PORT` | if not default | 7496 TWS live · 7497 TWS paper · 4001/4002 Gateway |
| `MOI_TELEGRAM_BOT_TOKEN` / `MOI_TELEGRAM_CHAT_ID` | optional | urgent alerts |
| `MOI_ALLOW_LIVE` | opt-in, default false | lets `moi execute` trade a live (`U…`) account; per-order/daily caps still apply |
| `MOI_TRADING_UNLOCK_KEY` | recommended for live | arming rail: live execution requires `moi unlock` (or the dashboard unlock), opening a 60-min window — generate with `openssl rand -hex 24` |

Machine-specific non-secret overrides go in `config/settings.local.yaml`
(see `config/settings.example.yaml`). Precedence: env vars > `.env` >
`settings.local.yaml` > `settings.yaml`.

## 3. Interactive Brokers

1. Install **Trader Workstation** (or IB Gateway) and log in.
   - *Reading your live account* (reports, snapshots) is safe: use your live login
     with **Read-Only API** checked.
   - *Executing approved orders* requires the **paper** account (`DU…` login) — the
     executor refuses live accounts unless `allow_live: true` is set deliberately.
2. Enable the API: *File → Global Configuration → API → Settings*
   - ☑ Enable ActiveX and Socket Clients
   - ☑ **Read-Only API** (uncheck only on the paper account when you want fills)
   - Socket port matching your `.env`; add `127.0.0.1` to Trusted IPs
3. Verify: `moi ibkr ping`

## 4. First run

```bash
moi db init            # create DuckDB + apply migrations
moi collect all        # all sources; congress/macro skip without keys
moi status             # freshness board — aim for green
moi weekly             # full pipeline + agent-written report (or --no-llm)
moi dashboard          # browse it — start at Mission control
```

From here on you rarely need the terminal: the dashboard's **Mission control** page
shows every connection and data source green/red and runs any pipeline command
(collect, report, full `moi run`, fill sync, …) as a background job with a live log
(written to `data/joblogs/`).

Notes:
- **DuckDB is single-writer** — don't run two `moi` pipeline processes at once.
  Mission control refuses to launch a job while another process holds the lock, the
  scheduled jobs wait up to 15 minutes for a competing writer, and data pages degrade
  to a "database busy" notice while a job runs.
- Price collection is incremental **per ticker** (new/lagging tickers heal
  automatically) and self-repairs after splits/dividend restatements by comparing the
  refetch overlap and re-pulling a diverged ticker's full history. `--full` remains
  for a complete rebuild.
- **Executing live orders:** `moi execute` prints the whole batch and asks for typed
  confirmation on a live account; with `MOI_TRADING_UNLOCK_KEY` set, you must also
  `moi unlock` (or use the dashboard sidebar) first — the window auto-relocks after
  60 minutes. `moi kill on` (or `data/KILL`) blocks everything regardless.
- Streamlit's first launch asks for an email on stdin; this repo's setup writes
  `~/.streamlit/credentials.toml` to skip it (done automatically if you used the
  bundled instructions).

## 5. Scheduling (macOS launchd)

Nightly data refresh — `~/Library/LaunchAgents/com.moi.collect.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.moi.collect</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-lc</string>
    <string>cd ~/Projects/my-own-investor && conda run -n my-own-investor moi collect all >> data/collect.log 2>&1</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer>
  </dict>
</dict></plist>
```

Weekly report — same pattern as `com.moi.weekly.plist` with
`moi weekly --collect >> data/weekly.log 2>&1` and:

```xml
<key>StartCalendarInterval</key><dict>
  <key>Weekday</key><integer>6</integer>
  <key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer>
</dict>
```

Daily urgent watcher (`moi watch`, 17:30 on weekdays) is optional but cheap.
Load each with `launchctl load ~/Library/LaunchAgents/<name>.plist`.

## 6. Development

```bash
ruff check . && ruff format --check .   # lint / format
mypy                                     # strict type-check
pytest                                   # 58 tests, no network needed
moi backtest run                         # gated walk-forward backtest
```

Conventions: every phase's acceptance gate is in
[IMPLEMENTATION.md](IMPLEMENTATION.md); collectors are idempotent upserts; anything
touching money lives behind the executor's safety rails and its tests.
