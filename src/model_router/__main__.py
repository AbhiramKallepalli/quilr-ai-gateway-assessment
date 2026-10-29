"""Run the router gateway: python -m model_router"""

import logging
import os

import uvicorn

from .app import Settings, create_app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    uvicorn.run(
        create_app(Settings.from_env()),
        host=os.environ.get("ROUTER_HOST", "127.0.0.1"),
        port=int(os.environ.get("ROUTER_PORT", "8100")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
