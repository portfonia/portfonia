"""Suppress URL-bearing library transport logs only during intel requests."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_active: ContextVar[bool] = ContextVar("intel_http_active", default=False)


class _TransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _active.get()


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_TransportFilter())


@contextmanager
def quiet_transport() -> Iterator[None]:
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)
