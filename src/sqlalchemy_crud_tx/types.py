"""Type aliases and Session-related protocol definitions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Protocol, TypeAlias, TypeVar, runtime_checkable

from sqlalchemy.orm import Session as _Session
from sqlalchemy.orm import scoped_session as _ScopedSession


@runtime_checkable
class ORMModel(Protocol):
    """Minimal capabilities required for ORM models.

    Designed to be compatible with plain SQLAlchemy declarative models
    without importing concrete base classes.
    """

    __table__: ClassVar[Any]


ModelTypeVar = TypeVar("ModelTypeVar", bound=ORMModel)
EntityTypeVar = TypeVar("EntityTypeVar")

ErrorLogger = Callable[..., None]

# In this library, ``SessionLike`` is treated as a SQLAlchemy ORM ``Session``
# or a ``scoped_session[Session]`` wrapper. This lets IDEs and type checkers
# reuse SQLAlchemy's own type hints while keeping runtime integration flexible.
SessionLike: TypeAlias = _Session | _ScopedSession[_Session]

SessionProvider = Callable[[], SessionLike]
