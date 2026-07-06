# Local setup

## 1. Environment

```bash
conda env create -f environment.yaml
conda activate my-own-investor
pip install -e ".[dev]"
pre-commit install
```

## 2. Configuration

```bash
cp config/settings.example.yaml config/settings.local.yaml   # optional local overrides
cp .env.example .env                                         # secrets (gitignored)
```

Edit `.env` for API keys (Phase 1+) and, if needed, IBKR overrides. Defaults in
`config/settings.yaml` assume TWS paper on port 7497.

## 3. Interactive Brokers (paper account)

1. Open a free IBKR account and enable the **paper trading** account.
2. Install **IB Gateway** (lighter) or **Trader Workstation (TWS)**, log in to the
   *paper* account.
3. Enable the API: TWS/Gateway → *Settings → API → Settings*:
   - check **Enable ActiveX and Socket Clients**
   - check **Read-Only API** (Phase 0 — we never place orders yet)
   - Socket port: **7497** (TWS paper) or **4002** (Gateway paper) — match
     `config/settings.yaml` / `.env`
   - add `127.0.0.1` to **Trusted IPs**
4. Verify connectivity:

```bash
moi ibkr ping
```

You should see the account id and (empty) positions.

## 4. First data pull

```bash
moi db init            # create DB + apply migrations
moi universe sync      # load config/universe.yaml
moi collect prices     # backfill ~3y daily OHLCV via yfinance (no gateway needed)
moi status             # freshness board
```

`moi collect prices --source ibkr` uses IBKR historical data instead (requires a live
gateway connection and market-data permissions).

## 5. Full data refresh (Phase 1)

```bash
moi collect all      # prices → 13F → form4 → congress → polymarket → news → macro
moi status           # green/red freshness board per table
```

Notes:
- **DuckDB is single-writer**: never run two `moi collect ...` processes at the same
  time. `moi collect all` runs everything sequentially in one process for this reason.
- Congress trades and FRED macro are **skipped** (yellow on the board) until you set
  `MOI_QUIVER_API_KEY` (or `MOI_UNUSUALWHALES_API_KEY`) and `MOI_FRED_API_KEY` in `.env`.
- Polymarket slugs in `config/polymarket.yaml` expire as markets resolve — refresh them
  occasionally via `https://gamma-api.polymarket.com/public-search?q=<term>`.

## 6. Nightly schedule (macOS launchd)

Create `~/Library/LaunchAgents/com.moi.collect.plist`:

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

Then: `launchctl load ~/Library/LaunchAgents/com.moi.collect.plist`.
Check the next morning with `moi status`.

## 7. Checks

```bash
ruff check .
mypy
pytest
```
