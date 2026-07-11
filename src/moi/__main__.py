"""Allow ``python -m moi`` — used by the dashboard to launch CLI jobs as subprocesses."""

from moi.cli import app

if __name__ == "__main__":
    app()
