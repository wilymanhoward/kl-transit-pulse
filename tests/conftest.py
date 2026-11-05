import io
import zipfile
from pathlib import Path

import pytest

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
