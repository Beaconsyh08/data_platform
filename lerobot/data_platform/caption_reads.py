"""Bounded, transient read requests relayed through authenticated outbound Agents."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

MAX_REPLY_BYTES = 8 * 1024 * 1024
VIDEO_CHUNK_BYTES = 4 * 1024 * 1024


@dataclass
class CaptionRead:
    node_id: str
    payload: dict
    ready: threading.Event = field(default_factory=threading.Event)
    claimed: bool = False
    status: int = 503
    body: bytes = b""
    binary: bool = False


class CaptionReadRelay:
    """Keep only in-flight replies in memory; never publish or retain dataset results."""

    def __init__(self, *, timeout: float = 30):
        self.timeout = timeout
        self.lock = threading.Lock()
        self.pending: dict[str, CaptionRead] = {}

    def request(self, node_id: str, payload: dict, *, timeout: float | None = None) -> CaptionRead:
        request_id = str(uuid.uuid4())
        read = CaptionRead(node_id, payload)
        with self.lock:
            if len(self.pending) >= 32:
                raise TimeoutError("Caption read capacity is busy; retry shortly")
            self.pending[request_id] = read
        try:
            if not read.ready.wait(self.timeout if timeout is None else timeout):
                raise TimeoutError("Dataset Agent did not respond to the caption read")
            return read
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def claim(self, node_id: str) -> dict | None:
        with self.lock:
            for request_id, read in self.pending.items():
                if read.node_id == node_id and not read.claimed:
                    read.claimed = True
                    return {"request_id": request_id, **read.payload}
        return None

    def complete(self, request_id: str, node_id: str, status: int, body: bytes, *, binary: bool) -> None:
        if status not in {200, 400, 403, 404, 409, 416, 422, 500} or len(body) > MAX_REPLY_BYTES:
            raise ValueError("Invalid caption read reply")
        with self.lock:
            read = self.pending.get(request_id)
            if read is None or read.node_id != node_id or not read.claimed or read.ready.is_set():
                raise KeyError("Caption read is no longer pending for this node")
            read.status, read.body, read.binary = status, body, binary
            read.ready.set()
