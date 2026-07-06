# my-own-investor (`moi`)

AI-assisted, human-approved portfolio copilot for Interactive Brokers, focused on
mid-horizon growth investments in small/mid-cap hardware for computing, data centers,
and connectivity.

- **Plan & architecture:** [docs/PLAN.md](docs/PLAN.md)
- **Phased build plan:** [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)
- **Local setup:** [docs/SETUP.md](docs/SETUP.md)

> Not financial advice. This is a personal tool that produces model output for your own
> review. It never trades without your explicit per-order approval.

## Quickstart

```bash
# 1. Create the environment
conda env create -f environment.yaml
conda activate my-own-investor
pip install -e ".[dev]"

# 2. Configure
cp config/settings.example.yaml config/settings.local.yaml
cp .env.example .env            # then edit with your IBKR port / API keys

# 3. Sanity checks
moi --help
moi ibkr ping                   # requires IB Gateway running (see docs/SETUP.md)
moi collect prices              # backfills OHLCV for the seed universe
moi status                      # data freshness board
```

## Project status

Phase 0 (foundations) — in progress. See [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md).
