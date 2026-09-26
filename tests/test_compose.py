"""WP-67: compose and Makefile hygiene, and the device-token docs."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def _recipe(target: str) -> str:
    text = (ROOT / "Makefile").read_text()
    match = re.search(rf"^{re.escape(target)}:.*?\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert match, target
    return match.group(1)


def test_syncthing_is_behind_a_profile_and_the_api_is_capped():
    services = _compose()["services"]
    assert services["syncthing"]["profiles"] == ["syncthing"]
    assert "mem_limit" in services["api"]


def test_backup_and_restore_never_start_the_stack():
    assert "--no-deps" in _recipe("backup")
    assert "--no-deps" in _recipe("restore")


def test_update_backs_up_stops_the_writers_and_keeps_syncthing():
    recipe = _recipe("update")
    assert recipe.index("$(MAKE) backup") < recipe.index("git pull --ff-only")
    assert recipe.index("stop bot worker userbot") < recipe.index("$(MAKE) up")
    assert "--profile syncthing up -d syncthing" in recipe
    assert "docker image prune -f" in recipe


def test_the_device_token_docs_name_the_money_routes():
    text = (ROOT / "android" / "README.md").read_text()
    assert "/v1/phone/sms" in text and "/v1/phone/notifications" in text
