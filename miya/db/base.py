"""Declarative base and shared column helpers."""

from __future__ import annotations

import enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def pg_enum(py_enum: type[enum.Enum], name: str) -> sa.Enum:
    """A native PostgreSQL enum whose labels are the member *values*.

    Without ``values_callable`` SQLAlchemy stores member *names*, which would
    turn ``Direction.in_`` into the label ``in_`` instead of ``in``.

    Call this once per enum and reuse the returned instance across every column
    that needs it — two distinct instances sharing a type name each try to
    ``CREATE TYPE`` during ``create_all()``.
    """
    return sa.Enum(
        py_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        native_enum=True,
    )


def created_at_column(**kwargs: Any):
    return mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
        **kwargs,
    )


def money_column(**kwargs: Any):
    """Money is NUMERIC(14,2) — never float."""
    return mapped_column(sa.Numeric(14, 2), **kwargs)


__all__ = ["Base", "created_at_column", "money_column", "pg_enum"]
