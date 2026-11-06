import os

from sqlalchemy import Engine, create_engine

# Local-dev default matches docker-compose.yml; deployments override DATABASE_URL.
# 127.0.0.1, not localhost: Postgres is bound to IPv4 loopback only, and on Windows
# "localhost" tries IPv6 ::1 first, which hangs instead of falling back.
DEFAULT_URL = "postgresql+psycopg://transit:transit@127.0.0.1:5432/transit"


def database_url() -> str:
    # Read at call time (not import time) so tests can point it elsewhere.
    return os.getenv("DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url())
