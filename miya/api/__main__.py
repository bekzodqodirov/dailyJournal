"""Entry point of the api container: ``python -m miya.api``.

Checks the configuration first, so a blank or short API_BEARER_TOKEN ends the
process with one Uzbek line instead of a Starlette/uvicorn traceback.
"""

from __future__ import annotations

import uvicorn

from miya.api.main import assert_startup_config, configure_logging

# Inside the container the api always listens on 8000; API_PORT in .env only
# picks the host port Compose maps onto it.
CONTAINER_PORT = 8000


def main() -> None:
    configure_logging()
    assert_startup_config()
    uvicorn.run("miya.api.main:app", host="0.0.0.0", port=CONTAINER_PORT)


if __name__ == "__main__":
    main()
