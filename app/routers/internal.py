"""内部 API：仅 EliCloud 内部服务调用，Bearer + 私网来源。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import get_db
from ..errors import ServiceError
from ..mailer import Mailer
from ..models import parse_iso
from ..schemas import VerifyCreate, VerifyCreated, VerifyStatus
from ..security import constant_time_equals, parse_bearer
from ..service import create_verification, get_request_by_id, status_for

router = APIRouter(prefix="/v1", tags=["internal"])


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


def app_mailer(request: Request) -> Mailer:
    return request.app.state.mailer


def require_internal_token(
    request: Request, settings: Settings = Depends(app_settings)
) -> str:
    if not settings.email_internal_token:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "internal_api_disabled",
                "error_description": "EMAIL_INTERNAL_TOKEN 未配置，内部接口已关闭",
            },
        )
    token = parse_bearer(request.headers.get("authorization"))
    if not token or not constant_time_equals(settings.email_internal_token, token):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthorized",
                "error_description": "缺少或无效的内部调用令牌",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


@router.post(
    "/verify",
    response_model=VerifyCreated,
    status_code=202,
    dependencies=[Depends(require_internal_token)],
)
def create_verify(
    payload: VerifyCreate,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(app_settings),
    mailer: Mailer = Depends(app_mailer),
) -> VerifyCreated:
    try:
        record = create_verification(db, settings, mailer, payload)
    except ServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.error, "error_description": exc.description},
            headers=exc.headers,
        ) from exc
    expires_in = int((parse_iso(record.expires_at) - parse_iso(record.created_at)).total_seconds())
    response.headers["Cache-Control"] = "no-store"
    return VerifyCreated(
        id=record.id,
        status=record.status,
        expires_in=expires_in,
        expires_at=record.expires_at,
    )


@router.get(
    "/verify/{request_id}",
    response_model=VerifyStatus,
    dependencies=[Depends(require_internal_token)],
)
def read_verify(
    request_id: str,
    response: Response,
    db: Session = Depends(get_db),
) -> VerifyStatus:
    try:
        record = get_request_by_id(db, request_id)
    except ServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.error, "error_description": exc.description},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return status_for(db, record)
