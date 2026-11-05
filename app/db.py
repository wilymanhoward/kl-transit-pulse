import os

from sqlalchemy import Engine, create_engine

# Local-dev default matches docker-compose.yml; deployments override DATABASE_URL.
DEFAULT_URL = "postgresql+psycopg://transit:transit@localhost:5432/transit"


def database_url() -> str:
    # Read at call time (not import time) so tests can point it elsewhere.
    return os.getenv("DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url())
