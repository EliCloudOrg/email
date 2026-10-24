"""公开入口：邮件一键确认、结果页、健康检查。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import __version__
from ..config import Settings
from ..db import get_db
from ..errors import ServiceError, client_ip, limiter
from ..service import confirm_by_token

router = APIRouter(prefix="/v1", tags=["public"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

RESULT_MESSAGES = {
    "approved": "邮箱已确认，可以关闭此页面。",
    "denied": "确认链接已超时，系统已视为拒绝。",
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
    """邮件里唯一的确认按钮指向这里：GET 即批准；超时则 denied。"""
    if not token:
        return _error_page(request, 400, "缺少确认令牌。")

    settings: Settings = request.app.state.settings
    ip = client_ip(request, settings) or "unknown"
    retry_after = limiter.allow(f"confirm:{ip}", settings.confirm_rate_limit_per_minute, 60)
    if retry_after is not None:
        response = _error_page(request, 429, "请求过于频繁，请稍后再试。", title="请求过于频繁")
        response.headers["Retry-After"] = str(int(retry_after) + 1)
        return response

    try:
        state, record = confirm_by_token(db, settings, token, ip)
    except ServiceError as exc:
        return _error_page(request, exc.status_code, exc.description)

    return _result_page(request, state, record, status_code=200)


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return JSONResponse(
            status_code=503, content={"status": "error", "database": "unavailable"}
        )
    return JSONResponse(content={"status": "ok", "version": __version__, "database": "ok"})
