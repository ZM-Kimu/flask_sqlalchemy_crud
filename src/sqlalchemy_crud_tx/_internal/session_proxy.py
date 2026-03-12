"""Internal sync session proxy used by CRUD.session."""

from __future__ import annotations

from typing import Any, Protocol

from ..types import ErrorLogger, SessionLike


class _SyncCRUDSessionOwner(Protocol):
    @property
    def logger(self) -> ErrorLogger: ...

    def commit(self) -> None: ...

    def discard(self) -> None: ...


class SessionProxy:
    """Session facade exposed to callers.

    Most members are delegated to the underlying session, but transaction
    control is redirected back to CRUD so callers do not bypass its state
    machine.
    """

    __slots__ = ("_crud", "_session")

    def __init__(self, crud: _SyncCRUDSessionOwner, session: SessionLike) -> None:
        self._crud = crud
        self._session = session

    def commit(self) -> None:
        self._crud.logger(
            "CRUD.session.commit() is redirected to CRUD.commit(); "
            "consider calling CRUD.commit() explicitly.",
        )
        self._crud.commit()

    def rollback(self) -> None:
        self._crud.logger(
            "CRUD.session.rollback() is redirected to CRUD.discard(); "
            "consider calling CRUD.discard() explicitly.",
        )
        self._crud.discard()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)
