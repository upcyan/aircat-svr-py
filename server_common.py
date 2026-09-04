"""Shared network safety helpers for the Web and Lite M1 servers."""

import socket
import time


FRAME_END_MARKER = b"#END#"


class FrameReadError(Exception):
    """Raised when a peer sends an invalid, oversized, or excessively slow frame."""


def recv_bounded_frame(conn, first_chunk, *, buffer_size, chunk_timeout,
                       max_frame_bytes, max_frame_seconds):
    """Read one M1 response without trusting TCP packet boundaries.

    The stock device terminates messages with ``#END#``. Responses without the
    marker remain compatible with the old idle-timeout behaviour, but an
    absolute size and time budget prevents slow-drip memory exhaustion.
    """
    data = bytearray(first_chunk)
    if len(data) > max_frame_bytes:
        raise FrameReadError("frame exceeds size limit")

    deadline = time.monotonic() + max_frame_seconds
    while FRAME_END_MARKER not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FrameReadError("frame exceeded total receive time")

        conn.settimeout(min(chunk_timeout, remaining))
        try:
            chunk = conn.recv(min(buffer_size, max_frame_bytes - len(data) + 1))
        except socket.timeout:
            break
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_frame_bytes:
            raise FrameReadError("frame exceeds size limit")

    return bytes(data)
