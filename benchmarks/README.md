# Benchmarks

This folder provides a reproducible CRUD vs raw SQLAlchemy benchmark baseline.

## Install

```bash
pip install -e ".[benchmark]"
```

## Run (PowerShell)

```powershell
$env:RUN_BENCHMARKS="1"
pytest -q benchmarks --benchmark-sort=mean --benchmark-columns=min,max,mean,stddev,rounds
```

## Optional: use external DB

Default is in-memory SQLite. To benchmark with another backend:

```powershell
$env:BENCH_DB="postgresql+psycopg://user:pass@localhost:5432/benchdb"
$env:RUN_BENCHMARKS="1"
pytest -q benchmarks --benchmark-sort=mean --benchmark-json=.benchmarks/latest.json
```

Notes:
- `BENCH_DB` also supports MySQL URL (`mysql://...`) and will be normalized to `mysql+pymysql://...`.
- Benchmarks are skipped unless `RUN_BENCHMARKS=1`, so default `pytest` stays fast.
- Cases currently cover:
  `add`,
  `add(instance=...)`,
  `get by pk`,
  `get by email`,
  `count`,
  `all`,
  `page-1 (count + limit + offset)`,
  `update(by id)`,
  `update(first)`,
  `delete(by id)`,
  `delete(by instance)`,
  `add_many`.
- Mutation cases (`update` / `delete`) force an explicit `flush` on both SA and CRUD paths before rollback/discard, so SQL side effects are comparable.
- Read/update/delete cases use equivalent SQLAlchemy 2.x statement shapes on both paths.
