"""The audit suite must never contact real provider endpoints."""

import httpx
import pytest


@pytest.fixture(autouse=True)
def block_live_http(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live HTTP is forbidden in tests; use MockTransport.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
