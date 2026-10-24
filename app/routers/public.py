"""公开入口：邮件一键确认（纯文本结果）、健康检查。

确认结果刻意不做 UI：点击邮件按钮后只返回一行文字。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import __version__
from ..config import Settings
from ..db import get_db
from ..errors import ServiceError, client_ip, limiter
from ..service import confirm_by_token

router = APIRouter(prefix="/v1", tags=["public"])

RESULT_MESSAGES = {
    "approved": "邮箱确认成功。",
    "denied": "确认链接已超时，已视为拒绝。",
    "failed": "邮件发送失败，请重新发起。",
}


def _text(message: str, status_code: int = 200, headers: dict[str, str] | None = None) -> PlainTextResponse:
    return PlainTextResponse(message, status_code=status_code, headers=headers)


@router.get("/confirm", response_class=PlainTextResponse)
def confirm(
    request: Request,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """邮件里唯一的确认按钮指向这里：GET 即批准；超时则 denied。"""
    if not token:
        return _text("缺少确认令牌。", status_code=400)

    settings: Settings = request.app.state.settings
    ip = client_ip(request, settings) or "unknown"
    retry_after = limiter.allow(f"confirm:{ip}", settings.confirm_rate_limit_per_minute, 60)
    if retry_after is not None:
        return _text(
            "请求过于频繁，请稍后再试。",
            status_code=429,
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    try:
        state, _record = confirm_by_token(db, settings, token, ip)
    except ServiceError as exc:
        return _text(exc.description, status_code=exc.status_code)

    return _text(RESULT_MESSAGES.get(state, "该链接已处理。"))


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return JSONResponse(
            status_code=503, content={"status": "error", "database": "unavailable"}
        )
    return JSONResponse(content={"status": "ok", "version": __version__, "database": "ok"})
