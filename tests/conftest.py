import io
import os
import zipfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "gtfs_mini"


def make_zip(overrides: dict[str, str | None] | None = None) -> bytes:
    """Zip the fixture feed. overrides: {filename: new text, or None to drop the file}."""
    files = {p.name: p.read_text(encoding="utf-8") for p in FIXTURE_DIR.glob("*.txt")}
    files.update(overrides or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            if text is not None:
                zf.writestr(name, text)
    return buf.getvalue()


@pytest.fixture
def mini_zip() -> bytes:
    return make_zip()


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch):
    """A fresh schema in the test database, built through Alembic (so migrations are
    tested too). Skips the test if TEST_DATABASE_URL isn't set."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(ROOT / "alembic.ini"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    yield eng
    eng.dispose()
