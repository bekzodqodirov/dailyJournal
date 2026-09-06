"""One-time Telethon login (``make userbot-login``).

Prints a session string for ``TELETHON_SESSION``. Run it interactively — it
asks for the phone number, the login code and (if set) the 2FA password.

The session string is a **full credential** for the owner's Telegram account:
put it in `.env`, never in the repository, and treat a leak like a stolen
password (revoke from Telegram → Settings → Devices).
"""

from __future__ import annotations

import asyncio
import sys

from miya.config import settings


async def _login() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # Driven with an explicit event loop rather than Telethon's synchronous
    # wrapper: that wrapper is installed as a side effect of `import
    # telethon.sync`, an import no linter can tell from an unused one. Without
    # it every client call returns an un-awaited coroutine — which is exactly
    # how this tool once managed to sign in and then throw away the session.
    client = TelegramClient(
        StringSession(), settings.telethon_api_id, settings.telethon_api_hash
    )
    await client.start()
    try:
        session_string = client.session.save()

        # The credential goes out first, before anything that could fail.
        # Logging in mints a new authorised session on the account; if the
        # string is lost the login cannot be repeated, only redone — leaving
        # an orphan session behind each time.
        print("\nAdd this line to .env (keep it secret):\n")
        print(f"TELETHON_SESSION={session_string}\n")

        try:
            me = await client.get_me()
            print(f"Logged in as: {me.first_name or me.username or me.id}")
        except Exception as exc:  # cosmetic only — never lose the session over it
            print(f"(signed in; could not read the account name: {exc})")
    finally:
        await client.disconnect()


def main() -> int:
    if not (settings.telethon_api_id and settings.telethon_api_hash):
        print(
            "TELETHON_API_ID and TELETHON_API_HASH must be set first "
            "(get them from https://my.telegram.org → API development tools).",
            file=sys.stderr,
        )
        return 1

    asyncio.run(_login())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
