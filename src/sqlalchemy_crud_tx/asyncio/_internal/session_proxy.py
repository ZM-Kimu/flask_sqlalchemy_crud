"""Internal async session proxy used by asyncio.CRUD.session."""

from __future__ import annotations

from typing import Any, Protocol

from ...types import AsyncSessionLike, ErrorLogger


class _AsyncCRUDSessionOwner(Protocol):
    @property
    def logger(self) -> ErrorLogger: ...

    async def commit(self) -> None: ...

    async def discard(self) -> None: ...


class AsyncSessionProxy:
    """Async session facade exposed to callers."""

    __slots__ = ("_crud", "_session")

    def __init__(
        self, crud: _AsyncCRUDSessionOwner, session: AsyncSessionLike
    ) -> None:
        self._crud = crud
        self._session = session

    async def commit(self) -> None:
        self._crud.logger(
            "CRUD.session.commit() is redirected to CRUD.commit(); "
            "consider calling CRUD.commit() explicitly.",
        )
        await self._crud.commit()

    async def rollback(self) -> None:
        self._crud.logger(
            "CRUD.session.rollback() is redirected to CRUD.discard(); "
            "consider calling CRUD.discard() explicitly.",
        )
        await self._crud.discard()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)
