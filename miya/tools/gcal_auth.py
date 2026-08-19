"""One-time Google Calendar OAuth flow (``make gcal-auth``).

The VPS is headless, so the flow binds a loopback redirect server and prints
the consent URL instead of opening a browser. Run it through an SSH tunnel:

    ssh -L 8765:127.0.0.1:8765 <vps>
    make gcal-auth          # then open the printed URL locally

The resulting token (with refresh token) is written to GOOGLE_TOKEN_JSON;
worker jobs pick it up on their next run — no restart needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from miya.config import settings
from miya.services.gcal import GCAL_SCOPES


def main() -> int:
    client_json = Path(settings.google_oauth_client_json)
    if not client_json.is_file():
        print(
            f"OAuth client file not found: {client_json}\n"
            "Create an OAuth 'Desktop app' client in Google Cloud Console "
            "(Calendar API enabled) and save its JSON there.",
            file=sys.stderr,
        )
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_json), GCAL_SCOPES)
    credentials = flow.run_local_server(
        # host feeds the redirect URI (the owner's browser must hit
        # localhost); bind_addr is the actual listening socket. Binding
        # container-loopback would make Docker's published port unreachable —
        # the consent redirect would spin forever.
        host="localhost",
        bind_addr="0.0.0.0",
        port=8765,
        open_browser=False,
        authorization_prompt_message=(
            "\nOpen this URL in your local browser "
            "(with `ssh -L 8765:127.0.0.1:8765 <vps>` active):\n\n{url}\n"
        ),
    )

    token_path = Path(settings.google_token_json)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json())
    token_path.chmod(0o600)
    print(f"Token saved to {token_path}. Calendar sync is now active.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
