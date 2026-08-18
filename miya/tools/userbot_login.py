"""One-time Telethon login (``make userbot-login``).

Prints a session string for ``TELETHON_SESSION``. Run it interactively — it
asks for the phone number, the login code and (if set) the 2FA password.

The session string is a **full credential** for the owner's Telegram account:
put it in `.env`, never in the repository, and treat a leak like a stolen
password (revoke from Telegram → Settings → Devices).
"""

from __future__ import annotations

import sys

from miya.config import settings


def main() -> int:
    if not (settings.telethon_api_id and settings.telethon_api_hash):
        print(
            "TELETHON_API_ID and TELETHON_API_HASH must be set first "
            "(get them from https://my.telegram.org → API development tools).",
            file=sys.stderr,
        )
        return 1

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    with TelegramClient(
        StringSession(), settings.telethon_api_id, settings.telethon_api_hash
    ) as client:
        session_string = client.session.save()
        me = client.get_me()
        print(f"\nLogged in as: {me.first_name or me.username}")
        print("\nAdd this line to .env (keep it secret):\n")
        print(f"TELETHON_SESSION={session_string}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
