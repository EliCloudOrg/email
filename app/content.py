"""邮件内容渲染：内置模板 + 自定义内容两条路。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Settings
from .constants import (
    CONTENT_MODE_CUSTOM,
    CONTENT_MODE_TEMPLATE,
    PURPOSE_LABELS,
)
from .schemas import EmailContent

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "email"

_HTML_ENV = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "htm"]),
    keep_trailing_newline=True,
)
_TEXT_ENV = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=False,
    keep_trailing_newline=True,
)

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")

# 系统占位符：调用方 variables 不能覆盖
SYSTEM_KEYS = frozenset(
    {
        "link",
        "email",
        "purpose",
        "purpose_label",
        "source",
        "expires_minutes",
        "request_id",
        "app_name",
    }
)

TEMPLATE_SUBJECTS: dict[str, str] = {
    "default": "{app_name} 邮箱确认",
    "register": "完成 {app_name} 注册：请确认邮箱",
}

MAX_SUBJECT = 200
MAX_TEXT = 20000
MAX_HTML = 50000


class ContentError(ValueError):
    """内容参数错误；service 会转成 400。"""


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str | None = None
    html: str | None = None


def _purpose_label(purpose: str) -> str:
    return PURPOSE_LABELS.get(purpose, purpose)


def _system_values(
    settings: Settings,
    *,
    request_id: str,
    token: str,
    email: str,
    purpose: str,
    source: str | None,
    created_at: datetime,
    expires_at: datetime,
) -> dict[str, str]:
    minutes = max(1, int((expires_at - created_at).total_seconds() // 60))
    return {
        "link": settings.confirm_url(token),
        "email": email,
        "purpose": purpose,
        "purpose_label": _purpose_label(purpose),
        "source": source or "",
        "expires_minutes": str(minutes),
        "request_id": request_id,
        "app_name": settings.app_name,
    }


def _merge_variables(
    content: EmailContent, system: dict[str, str]
) -> dict[str, str]:
    overlap = SYSTEM_KEYS.intersection(content.variables)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise ContentError(f"variables 不能覆盖系统占位符：{names}")
    return {**system, **content.variables}


def _resolve_template_name(settings: Settings, content: EmailContent, purpose: str) -> str:
    allowed = settings.allowed_template_list
    requested = (content.template or "").strip()
    if requested:
        name = requested
    elif purpose in allowed:
        name = purpose
    else:
        name = "default"
    if name not in allowed:
        raise ContentError(
            f"未知模板 {name!r}；允许：{', '.join(allowed) or '(空)'}"
        )
    return name


def _render_template(
    settings: Settings,
    content: EmailContent,
    ctx: dict[str, str],
) -> tuple[str, str, str | None]:
    name = ctx["_template_name"]
    html_text = _HTML_ENV.get_template(f"{name}.html").render(**ctx)
    text_text = _TEXT_ENV.get_template(f"{name}.txt").render(**ctx)
    subject = TEMPLATE_SUBJECTS.get(name, "{app_name} 邮箱确认").format(
        app_name=settings.app_name
    )
    if len(subject) > MAX_SUBJECT:
        raise ContentError("邮件主题超过 200 字符")
    return subject, html_text, text_text


def _render_custom(
    content: EmailContent, values: dict[str, str]
) -> tuple[str, str, str | None, str | None]:
    subject_raw = (content.subject or "").strip()
    text_raw = content.text
    html_raw = content.html

    pieces = {"subject": subject_raw}
    if text_raw is not None:
        pieces["text"] = text_raw
    if html_raw is not None:
        pieces["html"] = html_raw

    for label, value in pieces.items():
        for match in _PLACEHOLDER.finditer(value):
            name = match.group(1)
            if name not in values:
                raise ContentError(f"{label} 里的占位符 {{{{{name}}}}} 未知")

    def replace(value: str, *, escape: bool) -> str:
        def _sub(match: re.Match[str]) -> str:
            raw = values[match.group(1)]
            return html.escape(raw, quote=True) if escape else raw

        return _PLACEHOLDER.sub(_sub, value)

    subject = replace(subject_raw, escape=False)
    if any(ch in subject for ch in "\r\n"):
        raise ContentError("subject 替换后不能包含换行")
    if len(subject) > MAX_SUBJECT:
        raise ContentError("邮件主题超过 200 字符")

    text = replace(text_raw, escape=False) if text_raw is not None else None
    html_body = replace(html_raw, escape=True) if html_raw is not None else None

    if text is not None and len(text) > MAX_TEXT:
        raise ContentError("邮件纯文本正文超过 20KB")
    if html_body is not None and len(html_body) > MAX_HTML:
        raise ContentError("邮件 HTML 正文超过 50KB")
    if not (text or html_body):
        raise ContentError("custom 模式必须提供 text 或 html")
    return subject, html_body or "", text, html_body


def render_email(
    settings: Settings,
    *,
    request_id: str,
    token: str,
    email: str,
    purpose: str,
    source: str | None,
    content: EmailContent | None,
    created_at: datetime,
    expires_at: datetime,
) -> RenderedEmail:
    """按 content.mode 渲染；失败抛 ContentError（上层转 400）。"""
    content = content or EmailContent()
    system = _system_values(
        settings,
        request_id=request_id,
        token=token,
        email=email,
        purpose=purpose,
        source=source,
        created_at=created_at,
        expires_at=expires_at,
    )

    if content.mode == CONTENT_MODE_CUSTOM:
        values = _merge_variables(content, system)
        subject, html_body, text, _ = _render_custom(content, values)
        return RenderedEmail(subject=subject, text=text, html=html_body)

    name = _resolve_template_name(settings, content, purpose)
    values = _merge_variables(content, system)
    ctx = {**values, "_template_name": name}
    subject, html_body, text = _render_template(settings, content, ctx)
    return RenderedEmail(subject=subject, text=text, html=html_body)


def describe_content_mode(
    settings: Settings, content: EmailContent | None, purpose: str
) -> tuple[str, str | None]:
    """返回 (mode, template_name)，用于落库审计（不存正文）。"""
    content = content or EmailContent()
    if content.mode == CONTENT_MODE_CUSTOM:
        return CONTENT_MODE_CUSTOM, None
    return CONTENT_MODE_TEMPLATE, _resolve_template_name(settings, content, purpose)
