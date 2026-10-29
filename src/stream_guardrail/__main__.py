"""Run the guardrail gateway: python -m stream_guardrail"""

import logging
import os

import uvicorn

from .app import Settings, create_app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    uvicorn.run(
        create_app(Settings.from_env()),
        host=os.environ.get("GUARDRAIL_HOST", "127.0.0.1"),
        port=int(os.environ.get("GUARDRAIL_PORT", "8090")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
