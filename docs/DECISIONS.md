# Decisions

Short log: decision, why, alternative rejected.

## Phase 0

**Use `uv` for the virtualenv and dependencies.**
Why: already installed, one fast command (`uv sync`) creates the venv from `pyproject.toml`, and it writes a lock file so CI gets the same versions.
Rejected: plain `pip` + `requirements.txt` (no lock file by default, slower).

**Only add dependencies when a phase needs them.**
Why: Phase 0 only needs `httpx` and `gtfs-realtime-bindings`. FastAPI, SQLAlchemy and the rest come in later phases, so every dependency has a reason I can explain.
Rejected: installing the whole stack up front.

**Don't commit live samples (`data/samples/` is gitignored).**
Why: the static ZIP is 1.7 MB, it changes daily and anyone can re-download it. Tests will use small fixtures I build by hand in `tests/fixtures/`.
Rejected: committing real downloads.

**The inspector discovers populated fields by walking the protobuf (`ListFields`), not from a hard-coded list.**
Why: the point of Phase 0 is to find out what is actually there, without assuming.
Rejected: checking a fixed list of expected fields.

**Rate limit in the fetch script: exactly one request per API, one retry after 60 s on 429.**
Why: 1 request per API is far under 4/min. Real backoff belongs to the Phase 2 ingester.
Rejected: building full retry/backoff logic into a one-off script.

**Proposed: measure headway adherence instead of timetable delay.** (Needs Howard's OK, see DATA_FINDINGS.md.)
Why: `frequencies.txt` uses `exact_times=0` for 99.9% of trips, so there are no scheduled times to compare against.
Rejected: inventing scheduled times from the headway grid (only 2/88 live trips line up with it).
