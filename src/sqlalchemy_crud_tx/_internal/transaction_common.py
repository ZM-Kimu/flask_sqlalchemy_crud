"""Shared transaction policy types and error helpers."""

from __future__ import annotations

from typing import Literal, TypeAlias

from sqlalchemy.exc import InvalidRequestError

ErrorPolicy: TypeAlias = Literal["raise", "status_only"]
ExistingTxnPolicy: TypeAlias = Literal[
    "error",
    "join",
    "savepoint",
    "adopt_autobegin",
    "reset",
]


def raise_existing_txn_error(
    *,
    policy: ExistingTxnPolicy,
    origin: str | None,
    detail: str | None = None,
) -> None:
    """Raise a consistent InvalidRequestError for existing transaction conflicts."""
    origin_label = origin or "UNKNOWN"
    hint = (
        "Configure CRUD.configure(existing_txn_policy='join'|'savepoint'|"
        "'adopt_autobegin'|'reset') to change this behavior."
    )
    detail_text = f" {detail}" if detail else ""
    raise InvalidRequestError(
        "Session already has an active transaction "
        f"(origin={origin_label}). existing_txn_policy='{policy}' "
        f"disallows this operation.{detail_text} {hint}"
    )
