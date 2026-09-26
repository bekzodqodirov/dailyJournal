"""WP-73: the evening recap names MIYA's own blind spots."""

from __future__ import annotations

from datetime import datetime, timedelta

from miya.bot import recap_text
from miya.config import settings
from miya.services import health, recaps
from tests.recap_helpers import recap_of

NOW = datetime.now(settings.tz).replace(hour=19, minute=0, second=0, microsecond=0)


async def test_silent_phone_is_named(session, monkeypatch):
    monkeypatch.setattr(settings, "backup_age_recipient", "age1test")
    await health.beat(session, "phone_seen", now=NOW - timedelta(hours=30))

    text = await recap_of(session, now=NOW)

    assert recap_text.PHONE_SILENT_LINE.format(age="1 kun") in text


async def test_no_phone_ever_means_no_line(session):
    text = await recap_of(session, now=NOW)
    assert "📱 Telefon ilovasidan" not in text


async def test_unconfigured_backup_is_named(session, monkeypatch):
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    text = await recap_of(session, now=NOW)
    assert recap_text.BACKUP_UNCONFIGURED_LINE in text


async def test_problems_point_to_holat():
    lines = recap_text.system_lines(problems=True, backup_stale=True)
    assert lines == [recap_text.BACKUP_STALE_LINE, recap_text.PROBLEMS_LINE]


async def test_footer_failure_does_not_break_the_recap(session, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("health down")

    monkeypatch.setattr(health, "gather", boom)
    result = await recaps.build_evening(session, NOW.date(), now=NOW, store=False)

    assert result.parts
    assert recap_text.BACKUP_UNCONFIGURED_LINE not in "\n".join(result.parts)
