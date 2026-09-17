"""GET /vigil/vault — hard-disabled at this checkpoint (issue #451, P1.1).

No owner-authorization boundary exists yet (#452 is P1.2, not implemented).
Design comment: "keep the route unavailable rather than unauthenticated" —
so this checkpoint's route takes no auth dependency at all and always
returns 503, regardless of VIGIL_MODE or who calls it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_vigil_vault_returns_503(app_client: TestClient) -> None:
    resp = app_client.get("/vigil/vault")
    assert resp.status_code == 503
