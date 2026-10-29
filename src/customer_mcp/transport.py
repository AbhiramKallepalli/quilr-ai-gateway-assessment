"""Keeps stdout exclusively for JSON-RPC.

Configuring logging to use stderr is not enough on its own: a stray print(),
a third-party library or a C extension can still write to file descriptor 1
and corrupt the protocol stream. So before the server starts we:

1. duplicate the real stdout fd and hand that copy to the MCP transport, then
2. point fd 1 (and sys.stdout) at stderr.

After this, anything that writes to "stdout" lands in stderr, and only the
transport can write to the real stdout pipe.
"""

from __future__ import annotations

import io
import os
import sys

import anyio


def reserve_stdout_for_protocol() -> anyio.AsyncFile[str]:
    sys.stdout.flush()
    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr

    # newline="\n" so Windows doesn't turn message delimiters into \r\n.
    protocol_stream = io.TextIOWrapper(
        os.fdopen(protocol_fd, "wb"),
        encoding="utf-8",
        newline="\n",
        write_through=True,
    )
    return anyio.wrap_file(protocol_stream)
