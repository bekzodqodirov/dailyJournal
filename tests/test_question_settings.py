"""WP-16: the question budget's settings, their defaults and .env.example."""

from __future__ import annotations

import re
from pathlib import Path

from miya.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

DEFAULTS = {
    "question_budget_per_day": 10,
    "question_brief_slots": 5,
    "question_evening_slots": 3,
    "question_batch_max": 5,
    "question_push_gap_minutes": 120,
    "question_urgent_min_uzs": 5_000_000,
    "question_group_digest_size": 3,
    "question_group_max_shows": 2,
    "question_group_min_messages": 1,
    "question_ask_channels": False,
    "claim_ask_after_minutes": 10,
    "claim_duplicate_days": 14,
    "claim_bank_match_hours": 48,
    "claim_bank_autoclose_transactions": True,
    "claim_bank_autoaccept_settlements": False,
    "media_ask_in_groups": False,
    "media_ask_outgoing": False,
    "media_unasked_expiry_days": 7,
}


def test_the_defaults():
    fields = Settings.model_fields
    assert {key: fields[key].default for key in DEFAULTS} == DEFAULTS


def test_every_key_is_in_env_example_once_without_trailing_comments():
    lines = ENV_EXAMPLE.read_text().splitlines()
    for key in DEFAULTS:
        assert sum(1 for line in lines if line.startswith(f"{key.upper()}=")) == 1, key
    pattern = re.compile(
        r"^(QUESTION|CLAIM_BANK|CLAIM_ASK|CLAIM_DUP|MEDIA_ASK|MEDIA_UNASKED)[A-Z_]*=.*#"
    )
    assert [line for line in lines if pattern.match(line)] == []
