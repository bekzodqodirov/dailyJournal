"""Enumerations backed by native PostgreSQL enum types.

The `.value` of each member is what lands in the database; keep them stable —
changing a value requires a migration.
"""

from __future__ import annotations

import enum


class Currency(str, enum.Enum):
    UZS = "UZS"
    USD = "USD"
    CNY = "CNY"
    KRW = "KRW"
    RUB = "RUB"


class InteractionSource(str, enum.Enum):
    assistant_bot = "assistant_bot"
    telegram_userbot = "telegram_userbot"
    phone_call = "phone_call"
    manual = "manual"
    receipt_photo = "receipt_photo"
    calendar = "calendar"


class Direction(str, enum.Enum):
    """Direction of an interaction relative to the owner."""

    in_ = "in"
    out = "out"
    na = "na"


class ChatType(str, enum.Enum):
    private = "private"
    group = "group"
    channel = "channel"


class DebtDirection(str, enum.Enum):
    they_owe_me = "they_owe_me"
    i_owe_them = "i_owe_them"


class DebtStatus(str, enum.Enum):
    open = "open"
    partially_paid = "partially_paid"
    settled = "settled"


class PromiseMadeBy(str, enum.Enum):
    me = "me"
    them = "them"


class PromiseStatus(str, enum.Enum):
    open = "open"
    done = "done"
    broken = "broken"
    cancelled = "cancelled"


class TransactionType(str, enum.Enum):
    income = "income"
    expense = "expense"


class EventSource(str, enum.Enum):
    manual = "manual"
    extracted = "extracted"
    gcal = "gcal"


class EventStatus(str, enum.Enum):
    planned = "planned"
    done = "done"
    cancelled = "cancelled"


class TaskPriority(str, enum.Enum):
    low = "low"
    med = "med"
    high = "high"


class TaskStatus(str, enum.Enum):
    todo = "todo"
    doing = "doing"
    done = "done"
    dropped = "dropped"


# Names of the PostgreSQL types. Kept here so models and migrations agree.
CURRENCY_ENUM = "currency"
INTERACTION_SOURCE_ENUM = "interaction_source"
DIRECTION_ENUM = "direction"
CHAT_TYPE_ENUM = "chat_type"
DEBT_DIRECTION_ENUM = "debt_direction"
DEBT_STATUS_ENUM = "debt_status"
PROMISE_MADE_BY_ENUM = "promise_made_by"
PROMISE_STATUS_ENUM = "promise_status"
TRANSACTION_TYPE_ENUM = "transaction_type"
EVENT_SOURCE_ENUM = "event_source"
EVENT_STATUS_ENUM = "event_status"
TASK_PRIORITY_ENUM = "task_priority"
TASK_STATUS_ENUM = "task_status"
