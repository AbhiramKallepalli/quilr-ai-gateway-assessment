"""One structured audit event per gateway decision, ready for a SIEM."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

audit_logger = logging.getLogger("mcp_gateway.audit")


def audit(event: str, **fields: Any) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    record.update({k: v for k, v in fields.items() if v is not None})
    audit_logger.info(json.dumps(record, default=str))
