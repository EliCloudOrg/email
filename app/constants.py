"""服务内常量：状态、模式、purpose 展示名。

刻意只用普通字符串，SQLite 换 PostgreSQL 不需要改枚举。
"""

from __future__ import annotations

# ---- 验证请求状态 ---------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset(
    {STATUS_APPROVED, STATUS_DENIED, STATUS_EXPIRED, STATUS_FAILED}
)
# 会触发 callback 的状态（failed 不发：邮件都没发出去）
CALLBACK_STATUSES = frozenset({STATUS_APPROVED, STATUS_DENIED, STATUS_EXPIRED})

# ---- 邮件内容模式 ---------------------------------------------------------
CONTENT_MODE_TEMPLATE = "template"
CONTENT_MODE_CUSTOM = "custom"

# ---- Callback 投递状态 ----------------------------------------------------
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"

# ---- 事件 -----------------------------------------------------------------
CALLBACK_EVENT = "email.verification"

# ---- 时间格式 -------------------------------------------------------------
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# ---- purpose 展示名（页面/邮件里给人看） ----------------------------------
PURPOSE_LABELS: dict[str, str] = {
    "generic": "邮箱确认",
    "register": "注册",
    "reset-password": "重置密码",
    "change-email": "修改邮箱",
    "login": "登录确认",
    "security": "安全确认",
}
