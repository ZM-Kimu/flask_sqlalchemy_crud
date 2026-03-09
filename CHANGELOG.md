## v2.2.0 (2026-03-09)

### Fix

- **asyncio**: close provider-owned sessions and tighten transaction typing

## v2.1.0 (2026-03-05)

### Feat

- **asyncio**: add async CRUD namespace and release assets

## v2.0.2 (2026-02-28)

### Fix

- clean up typed transaction internals and contracts for 2.0.2

## v2.0.1 (2026-02-28)

### Fix

- replace the polluted previous release baseline with the corrected SQLAlchemy 2.x typed baseline.

### BREAKING CHANGE

- remove legacy Query core: `CRUD.query()`, `CRUDQuery`, and `configure(query_builder=...)`.
- remove built-in pagination API and public `PaginationResult` export.
- remove `delete(..., sync=...)` legacy Query-specific option.

### Feat

- switch to SQLAlchemy 2.x typed query path (`select/execute/scalars/scalar`).
- keep `first/all` with model `Select` support via `stmt` parameter.
- add pyright strict typing contracts and CI gate for `pytest + pyright`.

## v1.0.0 (2026-02-07)

### Fix

- **core**: fix the unexcept error cause by create_instance

## v0.3.0 (2026-01-06)

### BREAKING CHANGE

- package version bumped to 0.3.0; transaction behavior now configurable via existing_txn_policy, and apps hitting pre-existing transactions should set a policy explicitly.

### Feat

- **txn**: add existing_txn_policy and stabilize txn handling

## v0.2.0 (2026-01-05)

### BREAKING CHANGE

- package and import path rename to sqlalchemy_crud_tx, consider to update import path.

### Feat

- rename to sqlalchemy-crud-tx and polish docs/typing

## v0.1.0 (2025-12-17)

### Feat

- **crud**: abstract to sqlalchemy; remove strong dependence of flask & flask sqlalchemy

### Refactor

- **refactor**: refactor parameter and methods naming

## v0.0.3 (2025-12-07)

## v0.0.2 (2025-12-07)

## v0.0.1 (2025-12-07)
