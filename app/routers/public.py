"""公开页面：确认页、结果页、健康检查。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import __version__
from ..config import Settings
from ..constants import PURPOSE_LABELS
from ..db import get_db
from ..errors import ServiceError, client_ip, limiter
from ..service import confirm_page, decide

router = APIRouter(prefix="/v1", tags=["public"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

RESULT_MESSAGES = {
    "approved": "邮箱已确认，可以关闭此页面。",
    "denied": "已拒绝该邮箱确认。",
    "expired": "链接已过期，请回到原服务重新发起。",
    "failed": "该请求的邮件发送失败，请重新发起。",
}


def _render(name: str, request: Request, context: dict, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
    )


def _error_page(
    request: Request, status_code: int, message: str, *, title: str = "无法继续"
) -> HTMLResponse:
    return _render(
        "error.html",
        request,
        {
            "title": title,
            "message": message,
            "app_name": request.app.state.settings.app_name,
        },
        status_code=status_code,
    )


def _result_page(request: Request, status: str, record, status_code: int = 200) -> HTMLResponse:
    return _render(
        "result.html",
        request,
        {
            "title": "邮箱确认",
            "status": status,
            "message": RESULT_MESSAGES.get(status, "该链接已处理。"),
            "email": record.email,
            "app_name": request.app.state.settings.app_name,
        },
        status_code=status_code,
    )


@router.get("/confirm", response_class=HTMLResponse)
def confirm(
    request: Request,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not token:
        return _error_page(request, 400, "缺少确认令牌。")
    settings: Settings = request.app.state.settings
    try:
        state, record, csrf = confirm_page(db, settings, token)
    except ServiceError as exc:
        return _error_page(request, exc.status_code, exc.description)

    if state == "pending":
        return _render(
            "confirm.html",
            request,
            {
                "title": "确认邮箱",
                "token": token,
                "csrf": csrf,
                "email": record.email,
                "purpose": record.purpose,
                "purpose_label": PURPOSE_LABELS.get(record.purpose, record.purpose),
                "expires_at": record.expires_at,
                "app_name": settings.app_name,
            },
        )
    return _result_page(request, state, record, status_code=410 if state == "expired" else 200)


@router.post("/confirm", response_class=HTMLResponse)
def submit_confirm(
    request: Request,
    token: str = Form(...),
    decision: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    ip = client_ip(request, settings) or "unknown"
    retry_after = limiter.allow(f"confirm:{ip}", settings.confirm_rate_limit_per_minute, 60)
    if retry_after is not None:
        response = _error_page(request, 429, "请求过于频繁，请稍后再试。", title="请求过于频繁")
        response.headers["Retry-After"] = str(int(retry_after) + 1)
        return response

    try:
        state, record = decide(db, settings, token, decision, csrf, ip)
    except ServiceError as exc:
        return _error_page(request, exc.status_code, exc.description)

    return _result_page(request, state, record, status_code=410 if state == "expired" else 200)


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return JSONResponse(
            status_code=503, content={"status": "error", "database": "unavailable"}
        )
    return JSONResponse(content={"status": "ok", "version": __version__, "database": "ok"})
