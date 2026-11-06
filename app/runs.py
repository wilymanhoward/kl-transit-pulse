"""Record every ingestion attempt (static or realtime) in ingestion_runs.

Each write is its own short transaction, so a failed load still leaves a 'failed' row.
"""

from sqlalchemy import Engine, func, insert, update

from app.models import IngestionRun


def start_run(engine: Engine, kind: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            insert(IngestionRun).values(kind=kind).returning(IngestionRun.id)
        ).scalar_one()


def finish_run(engine: Engine, run_id: int, status: str, **values: object) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(IngestionRun)
            .where(IngestionRun.id == run_id)
            .values(status=status, finished_at=func.now(), **values)
        )


def fail_run(engine: Engine, run_id: int, exc: BaseException) -> None:
    finish_run(engine, run_id, "failed", error=repr(exc)[:2000])
