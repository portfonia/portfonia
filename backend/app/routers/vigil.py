from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.models.vigil import VigilRuntime, VigilVault
from app.schemas.vigil import (
    VigilArmIn,
    VigilArmOut,
    VigilCheckInIn,
    VigilCheckInOut,
    VigilConfigurationIn,
    VigilConfigurationOut,
    VigilDrillIn,
    VigilDrillOut,
    VigilObjectInitIn,
    VigilObjectInitOut,
    VigilObjectUploadOut,
    VigilVaultStatus,
)
from app.services.vigil.access import VigilOwner, require_vigil_owner
from app.services.vigil.arm import (
    VigilArmConflict,
    VigilArmInputError,
    VigilArmUnavailable,
    arm_pending,
)
from app.services.vigil.configuration import (
    VigilConfigurationInputError,
    VigilRecipientsLocked,
    VigilRevisionConflict,
    validate_configuration_input,
    write_pending_configuration,
)
from app.services.vigil.cycles import (
    VigilCycleConflict,
    VigilCycleUnavailable,
    check_in,
    current_deadline_at,
    disarm,
)
from app.services.vigil.delivery import ingest_verified_webhook, verify_resend_signature
from app.services.vigil.dns_check import (
    VigilDnsUnavailable,
    VigilNoMailRoute,
    check_recipients_dns,
)
from app.services.vigil.drills import (
    VigilDrillConflict,
    VigilDrillCooldown,
    VigilDrillInputError,
    VigilDrillUnavailable,
    drill_delivery_state,
    enqueue_drill,
)
from app.services.vigil.objects import (
    MAX_UPLOAD_BODY_BYTES,
    VigilObjectConflict,
    VigilObjectInputError,
    VigilObjectNotFound,
    init_object,
    upload_object,
)

router = APIRouter()

# Issue #452 (Vigil R0 P1.2): owner authorization now exists
# (services/vigil/access.py) — this route reads the caller's own vault row
# under it. vigil_configurations/vigil_objects exist as of #454, but this
# route doesn't decrypt/populate `active`/`pending`/`recipients` from them
# yet (no masking policy decided at this checkpoint — see schemas/vigil.py);
# `delivery_status` stays empty until #456+ (no outbox yet), and
# `last_scan_completed_at` stays None until a scan task writes it
# (#453/#456+).


@router.get("/vault", response_model=VigilVaultStatus)
def get_vault(
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilVaultStatus:
    vault = session.execute(
        select(VigilVault).where(VigilVault.owner_user_id == owner.user_id)
    ).scalar_one_or_none()

    if vault is None:
        return VigilVaultStatus(vault_id=None, phase="DISARMED", revision=0)

    runtime = session.get(VigilRuntime, 1)
    last_scan = runtime.last_scan_completed_at if runtime is not None else None
    return VigilVaultStatus(
        vault_id=vault.id,
        phase=vault.phase,
        revision=vault.revision,
        hold_reason=vault.hold_reason,
        next_check_at=vault.next_check_at,
        deadline_at=current_deadline_at(session, vault),
        last_scan_completed_at=last_scan,
        delivery_status=drill_delivery_state(session, vault),
    )


# Issue #454 (Vigil R0 P2.1): configuration + object storage. DNS routing
# validity happens outside any lock — see services/vigil/dns_check.py and
# #450 Design section 5 ("DNS checks occur outside locks; a transient DNS
# failure is 503 with no active-state change").


@router.post(
    "/configurations", response_model=VigilConfigurationOut, status_code=status.HTTP_201_CREATED
)
def post_configuration(
    payload: VigilConfigurationIn,
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilConfigurationOut:
    try:
        normalized = validate_configuration_input(
            interval_days=payload.interval_days,
            grace_hours=payload.grace_hours,
            recipients=[r.model_dump() for r in payload.recipients],
            message=payload.message,
        )
    except VigilConfigurationInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    try:
        check_recipients_dns(r.email for r in normalized.recipients)
    except VigilNoMailRoute as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except VigilDnsUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    try:
        result = write_pending_configuration(
            session,
            owner_user_id=owner.user_id,
            owner_email=owner.email,
            expected_revision=payload.expected_revision,
            normalized=normalized,
        )
    except VigilRevisionConflict as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "revision_conflict", "current_revision": exc.current_revision},
        ) from exc
    except VigilRecipientsLocked as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    session.commit()
    return VigilConfigurationOut(
        vault_id=result.vault_id, config_id=result.config_id, revision=result.revision
    )


@router.post(
    "/objects/init", response_model=VigilObjectInitOut, status_code=status.HTTP_201_CREATED
)
def post_object_init(
    payload: VigilObjectInitIn,
    response: Response,
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilObjectInitOut:
    try:
        result = init_object(
            session,
            owner_user_id=owner.user_id,
            expected_revision=payload.expected_revision,
            config_id=payload.config_id,
            request_id=payload.request_id,
            filename=payload.filename,
            plaintext_size=payload.plaintext_size,
        )
    except VigilObjectNotFound as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except VigilObjectInputError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except VigilObjectConflict as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except VigilRevisionConflict as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "revision_conflict", "current_revision": exc.current_revision},
        ) from exc

    session.commit()
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return VigilObjectInitOut(object_id=result.object_id, revision=result.revision)


_MAX_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


@router.post(
    "/objects/upload", response_model=VigilObjectUploadOut, status_code=status.HTTP_201_CREATED
)
async def post_object_upload(
    response: Response,
    expected_revision: int = Form(...),
    config_id: UUID = Form(...),
    object_id: UUID = Form(...),
    manifest: str = Form(...),
    inner: str = Form(...),
    file: UploadFile = File(...),
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilObjectUploadOut:
    try:
        manifest_obj = json.loads(manifest)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="manifest is not valid JSON"
        ) from exc

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_MAX_UPLOAD_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_UPLOAD_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"upload body exceeds {MAX_UPLOAD_BODY_BYTES} bytes",
            )
        chunks.append(chunk)
    ciphertext = b"".join(chunks)

    try:
        result = upload_object(
            session,
            owner_user_id=owner.user_id,
            expected_revision=expected_revision,
            object_id=object_id,
            config_id=config_id,
            manifest=manifest_obj,
            inner_b64=inner,
            ciphertext=ciphertext,
        )
    except VigilObjectNotFound as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except VigilObjectInputError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except VigilObjectConflict as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except VigilRevisionConflict as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "revision_conflict", "current_revision": exc.current_revision},
        ) from exc

    session.commit()
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return VigilObjectUploadOut(
        object_id=result.object_id, status=result.status, revision=result.revision
    )


@router.post("/drills", response_model=VigilDrillOut, status_code=status.HTTP_202_ACCEPTED)
def post_drill(
    payload: VigilDrillIn,
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilDrillOut:
    try:
        result = enqueue_drill(
            session,
            owner_user_id=owner.user_id,
            expected_revision=payload.expected_revision,
            config_id=payload.config_id,
            object_id=payload.object_id,
        )
        session.commit()
    except VigilRevisionConflict as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "revision_conflict", "current_revision": exc.current_revision},
        ) from exc
    except VigilDrillCooldown as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="drill cooldown",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except VigilDrillInputError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except (VigilDrillUnavailable, VigilDrillConflict) as exc:
        session.rollback()
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if isinstance(exc, VigilDrillUnavailable)
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    try:
        from app.tasks.vigil_tasks import dispatch_vigil_outbox_task

        dispatch_vigil_outbox_task.delay()
    except Exception:
        # Sweep recovers a lost enqueue; do not fail the accepted drill.
        pass
    return VigilDrillOut(drill_id=result.drill_id, status=result.status, revision=result.revision)


@router.post("/arm", response_model=VigilArmOut)
def post_arm(
    payload: VigilArmIn,
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilArmOut:
    try:
        result = arm_pending(
            session,
            owner_user_id=owner.user_id,
            expected_revision=payload.expected_revision,
            config_id=payload.config_id,
            object_id=payload.object_id,
        )
        session.commit()
    except VigilRevisionConflict as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "revision_conflict", "current_revision": exc.current_revision},
        ) from exc
    except VigilArmInputError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except VigilArmConflict as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except VigilArmUnavailable as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return VigilArmOut(
        phase=result.phase, revision=result.revision, next_check_at=result.next_check_at
    )


def _cycle_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VigilRevisionConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "revision_conflict", "current_revision": exc.current_revision},
        )
    if isinstance(exc, VigilCycleConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail)
    if isinstance(exc, VigilCycleUnavailable):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    raise exc


@router.post("/check-in", response_model=VigilCheckInOut)
def post_check_in(
    payload: VigilCheckInIn,
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilCheckInOut:
    try:
        result = check_in(
            session, owner_user_id=owner.user_id, expected_revision=payload.expected_revision
        )
        session.commit()
    except (VigilCycleConflict, VigilCycleUnavailable, VigilRevisionConflict) as exc:
        session.rollback()
        raise _cycle_http_error(exc) from exc
    return VigilCheckInOut(
        phase=result.phase, revision=result.revision, next_check_at=result.next_check_at
    )


@router.post("/disarm", response_model=VigilCheckInOut)
def post_disarm(
    payload: VigilCheckInIn,
    owner: VigilOwner = Depends(require_vigil_owner),
    session: Session = Depends(get_session),
) -> VigilCheckInOut:
    try:
        result = disarm(
            session, owner_user_id=owner.user_id, expected_revision=payload.expected_revision
        )
        session.commit()
    except (VigilCycleConflict, VigilCycleUnavailable, VigilRevisionConflict) as exc:
        session.rollback()
        raise _cycle_http_error(exc) from exc
    return VigilCheckInOut(
        phase=result.phase, revision=result.revision, next_check_at=result.next_check_at
    )


def _svix_headers(request: Request) -> dict[str, str]:
    headers = request.headers
    return {
        "id": headers.get("svix-id") or headers.get("webhook-id") or "",
        "timestamp": headers.get("svix-timestamp") or headers.get("webhook-timestamp") or "",
        "signature": headers.get("svix-signature") or headers.get("webhook-signature") or "",
    }


@router.post("/webhooks/resend", status_code=status.HTTP_200_OK)
async def post_resend_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """Raw signed Resend/Svix body. Verify BEFORE parsing. Duplicate = 200
    no-op; bad signature = 400; DB failure = 503. No owner session.
    """
    settings = get_settings()
    secret = settings.RESEND_WEBHOOK_SECRET
    if secret is None or not secret.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vigil webhook secret is not configured",
        )
    raw = await request.body()
    payload_text = raw.decode("utf-8")
    try:
        verify_resend_signature(
            payload=payload_text, headers=_svix_headers(request), secret=secret.get_secret_value()
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid webhook signature"
        ) from exc

    try:
        parsed: Any = json.loads(payload_text)
    except json.JSONDecodeError:
        return {"status": "ignored"}
    if not isinstance(parsed, dict):
        return {"status": "ignored"}

    event_id = _svix_headers(request)["id"]
    if not event_id:
        return {"status": "ignored"}
    try:
        result = ingest_verified_webhook(
            session,
            provider_event_id=event_id,
            payload=parsed,
            received_at=datetime.now(UTC),
        )
        session.commit()
    except OperationalError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable"
        ) from exc
    if result == "ignored":
        return {"status": "ignored"}
    return {"status": "ok"}
