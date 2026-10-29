"""Run the gateway: python -m mcp_gateway"""

import logging
import os

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    app = create_app(Settings.from_env())
    uvicorn.run(
        app,
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_PORT", "8080")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
