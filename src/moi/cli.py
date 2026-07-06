"""``moi`` command-line interface (typer)."""

from __future__ import annotations

import typer

from moi.config import get_settings
from moi.logging import configure_logging, get_logger

app = typer.Typer(
    name="moi",
    help="my-own-investor — AI-assisted, human-approved IBKR portfolio copilot.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database and migrations.", no_args_is_help=True)
ibkr_app = typer.Typer(help="Interactive Brokers connectivity.", no_args_is_help=True)
collect_app = typer.Typer(help="Data collectors.", no_args_is_help=True)
universe_app = typer.Typer(help="Investment universe.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(ibkr_app, name="ibkr")
app.add_typer(collect_app, name="collect")
app.add_typer(universe_app, name="universe")

log = get_logger(__name__)


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    settings = get_settings()
    configure_logging(level="DEBUG" if verbose else settings.log_level, json=settings.log_json)


# --------------------------------------------------------------------------- #
# db
# --------------------------------------------------------------------------- #
@db_app.command("init")
def db_init() -> None:
    """Create the database and apply all migrations."""
    from moi.db import connect

    con = connect()
    version = con.execute("SELECT max(version) FROM schema_version").fetchone()
    v = version[0] if version else 0
    typer.echo(f"Database ready at {get_settings().db_path} (schema v{v}).")


# --------------------------------------------------------------------------- #
# universe
# --------------------------------------------------------------------------- #
@universe_app.command("sync")
def universe_sync() -> None:
    """Load config/universe.yaml into the database."""
    from moi.db import connect
    from moi.universe import sync_universe

    con = connect()
    n = sync_universe(con)
    typer.echo(f"Synced {n} instruments into the universe table.")


@universe_app.command("list")
def universe_list() -> None:
    """Print the current universe."""
    from moi.db import connect

    con = connect()
    rows = con.execute(
        "SELECT ticker, sub_sector, is_benchmark FROM universe WHERE active "
        "ORDER BY is_benchmark, sub_sector, ticker"
    ).fetchall()
    for ticker, sub, bench in rows:
        tag = "benchmark" if bench else (sub or "-")
        typer.echo(f"  {ticker:6}  {tag}")
    typer.echo(f"\n{len(rows)} active instruments.")


# --------------------------------------------------------------------------- #
# ibkr
# --------------------------------------------------------------------------- #
@ibkr_app.command("ping")
def ibkr_ping() -> None:
    """Connect to IB Gateway/TWS, print account summary, disconnect."""
    from moi.ingest.ibkr import ping

    try:
        info = ping()
    except ConnectionError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.secho(f"Connected. Account: {info.account}", fg=typer.colors.GREEN)
    typer.echo(f"  Net liquidation: {info.net_liquidation}")
    typer.echo(f"  Total cash:      {info.total_cash}")
    typer.echo(f"  Open positions:  {len(info.positions)}")
    for sym, pos, cost in info.positions:
        typer.echo(f"    {sym:6} qty={pos:>10} avg_cost={cost}")


# --------------------------------------------------------------------------- #
# collect
# --------------------------------------------------------------------------- #
@collect_app.command("prices")
def collect_prices_cmd(
    source: str = typer.Option("yfinance", help="Price source: yfinance | ibkr."),
    years: int | None = typer.Option(None, help="History window; defaults to config."),
) -> None:
    """Backfill/refresh daily OHLCV for the universe."""
    from moi.db import connect
    from moi.ingest.prices import collect_prices
    from moi.universe import sync_universe

    settings = get_settings()
    con = connect()
    sync_universe(con)  # ensure universe table is current before collecting
    written = collect_prices(con, years=years or settings.price_history_years, source=source)
    typer.secho(f"Upserted {written} price rows from {source}.", fg=typer.colors.GREEN)


@collect_app.command("13f")
def collect_13f_cmd() -> None:
    """Fetch whale 13F holdings (config/whales.yaml) with QoQ change status."""
    from moi.db import connect
    from moi.ingest.edgar_13f import collect_13f

    written = collect_13f(connect())
    typer.secho(f"Upserted {written} 13F holding rows.", fg=typer.colors.GREEN)


@collect_app.command("form4")
def collect_form4_cmd(
    per_ticker: int = typer.Option(10, help="Recent Form 4 filings per ticker."),
) -> None:
    """Fetch insider Form 4 transactions for universe tickers."""
    from moi.db import connect
    from moi.ingest.edgar_form4 import collect_form4

    written = collect_form4(connect(), filings_per_ticker=per_ticker)
    typer.secho(f"Upserted {written} insider transaction rows.", fg=typer.colors.GREEN)


@collect_app.command("congress")
def collect_congress_cmd() -> None:
    """Fetch congressional trade disclosures (needs a Quiver/Unusual Whales API key)."""
    from moi.db import connect
    from moi.ingest.congress import collect_congress

    written = collect_congress(connect())
    typer.secho(f"Upserted {written} congress trade rows.", fg=typer.colors.GREEN)


@collect_app.command("polymarket")
def collect_polymarket_cmd() -> None:
    """Fetch Polymarket probability series (config/polymarket.yaml)."""
    from moi.db import connect
    from moi.ingest.polymarket import collect_polymarket

    written = collect_polymarket(connect())
    typer.secho(f"Upserted {written} probability points.", fg=typer.colors.GREEN)


@collect_app.command("news")
def collect_news_cmd() -> None:
    """Fetch news headlines (per-ticker + sector RSS feeds)."""
    from moi.db import connect
    from moi.ingest.news import collect_news

    written = collect_news(connect())
    typer.secho(f"Ingested {written} news items.", fg=typer.colors.GREEN)


@collect_app.command("macro")
def collect_macro_cmd() -> None:
    """Fetch FRED macro series (needs MOI_FRED_API_KEY)."""
    from moi.db import connect
    from moi.ingest.macro import collect_macro

    written = collect_macro(connect())
    typer.secho(f"Upserted {written} macro points.", fg=typer.colors.GREEN)


@collect_app.command("all")
def collect_all_cmd() -> None:
    """Run every collector in sequence (nightly job). Failures don't abort the run."""
    from moi.db import connect
    from moi.universe import sync_universe

    settings = get_settings()
    con = connect()
    sync_universe(con)

    def _step(name: str, fn: typing.Callable[[], int]) -> tuple[str, str]:
        try:
            return name, f"ok ({fn()} rows)"
        except Exception as exc:
            log.error("collect_step_failed", step=name, error=str(exc))
            return name, f"ERROR: {exc}"

    import typing

    from moi.ingest.congress import collect_congress
    from moi.ingest.edgar_13f import collect_13f
    from moi.ingest.edgar_form4 import collect_form4
    from moi.ingest.macro import collect_macro
    from moi.ingest.news import collect_news
    from moi.ingest.polymarket import collect_polymarket
    from moi.ingest.prices import collect_prices

    results = [
        _step("prices", lambda: collect_prices(con, years=settings.price_history_years)),
        _step("13f", lambda: collect_13f(con)),
        _step("form4", lambda: collect_form4(con)),
        _step("congress", lambda: collect_congress(con)),
        _step("polymarket", lambda: collect_polymarket(con)),
        _step("news", lambda: collect_news(con)),
        _step("macro", lambda: collect_macro(con)),
    ]
    typer.echo("\ncollect all — summary")
    failed = False
    for name, outcome in results:
        color = typer.colors.GREEN if outcome.startswith("ok") else typer.colors.RED
        if not outcome.startswith("ok"):
            failed = True
        typer.secho(f"  {name:12} {outcome}", fg=color)
    if failed:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@app.command("status")
def status() -> None:
    """Print a data-freshness board (green/red per table) and recent runs."""
    from moi.db import connect, scalar
    from moi.ingest.quality import check_freshness

    con = connect()
    universe_n = scalar(con, "SELECT count(*) FROM universe WHERE active")
    typer.echo("data status")
    typer.echo(f"  universe (active)  : {universe_n}")

    state_colors = {
        "ok": typer.colors.GREEN,
        "stale": typer.colors.RED,
        "empty": typer.colors.RED,
        "skipped": typer.colors.YELLOW,
    }
    for ts in check_freshness(con):
        typer.secho(
            f"  {ts.table:18} : {ts.state:7} {ts.rows:>7} rows, latest {ts.latest}",
            fg=state_colors.get(ts.state, typer.colors.YELLOW),
        )

    last_runs = con.execute(
        "SELECT job, status, rows_written, finished_at FROM run_log "
        "ORDER BY started_at DESC LIMIT 8"
    ).fetchall()
    if last_runs:
        typer.echo("recent runs")
        status_colors = {"ok": typer.colors.GREEN, "error": typer.colors.RED}
        for job, st, rows, fin in last_runs:
            c = status_colors.get(st, typer.colors.YELLOW)
            typer.secho(f"  {job:20} {st:7} rows={rows} at {fin}", fg=c)


if __name__ == "__main__":
    app()
