# KL Transit Pulse

Measures how reliable Kuala Lumpur's Rapid KL buses really are, using live GPS positions and the published timetable from Malaysia's open data portal.

> Status: Phase 0 (data reconnaissance). See [docs/DATA_FINDINGS.md](docs/DATA_FINDINGS.md).

## Quick start (so far)

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python scripts/fetch_samples.py    # 1 request per API
uv run python scripts/inspect_samples.py
```

## Data source and licence

Data from [data.gov.my](https://developer.data.gov.my) (GTFS Static and GTFS Realtime for Prasarana `rapid-bus-kl`), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Credit: **data.gov.my**.

## Limitations

- Reliability figures will be **estimates** derived from GPS positions, not official figures.
- The API allows 4 requests per minute.
- Rapid Rail (LRT/MRT) has no stable realtime feed, so it is out of scope.
