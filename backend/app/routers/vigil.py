from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter()

# Issue #451 (Vigil R0 P1.1): no owner-authorization boundary exists yet —
# #452 (P1.2) is what adds current_principal + the R0 owner-allowlist
# check. Per the P1.1 Design comment: "keep the route unavailable rather
# than unauthenticated" — this route intentionally takes NO auth
# dependency and always 503s, regardless of VIGIL_MODE or caller, rather
# than improvising a temporary auth check that #452 would have to unwind.


@router.get("/vault")
def get_vault() -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="vigil is not available yet",
    )
