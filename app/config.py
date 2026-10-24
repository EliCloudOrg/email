"""全部配置从环境变量读取。

设计要点：
- ``PUBLIC_BASE_URL`` 是对外地址的唯一来源；确认链接由它派生，代码里零硬编码域名/IP。
  本地是 ``http://127.0.0.1:8010``，生产是 ``https://146.56.237.33/email``。
- 内部 token / callback 签名密钥未配置时 fail closed，不让服务“裸奔”。
- ``MAIL_MODE=log`` 仅供本地/首次联调；生产用 smtp。
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote, urlsplit

from email_validator import EmailNotValidError, validate_email
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_RETRY_SCHEDULE = "0,30,120,600,3600,21600"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # ---- 对外地址（唯一需要随环境改动的一项） -------------------------------
    public_base_url: str = "http://127.0.0.1:8010"
    app_name: str = "EliCloud"

    # ---- 存储 ---------------------------------------------------------------
    database_url: str = "sqlite:////data/email.db"

    # ---- 内部调用鉴权 / callback 签名 --------------------------------------
    email_internal_token: str = ""
    callback_signing_secret: str = ""
    callback_allowed_hosts: str = "127.0.0.1,localhost"

    # ---- 邮件 ---------------------------------------------------------------
    mail_mode: str = "log"  # log | smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_timeout: float = 10.0
    mail_from: str = "no-reply@elipese.cloud"
    mail_from_name: str = "EliCloud"
    allowed_templates: str = "default,register"

    # ---- 验证请求 -----------------------------------------------------------
    verification_ttl_seconds: int = 900
    max_active_per_email: int = 3
    create_cooldown_seconds: int = 60
    confirm_rate_limit_per_minute: int = 10
    metadata_max_bytes: int = 8192

    # ---- Callback -----------------------------------------------------------
    callback_max_attempts: int = 6
    callback_retry_schedule: str = DEFAULT_RETRY_SCHEDULE
    callback_timeout_seconds: float = 10.0
    callback_on_expiry: bool = True

    # ---- Worker / 清理 ------------------------------------------------------
    worker_enabled: bool = True
    worker_interval_seconds: float = 5.0
    retention_days: int = 30

    # ---- 部署/代理 ----------------------------------------------------------
    trust_proxy_headers: bool = True
    log_level: str = "info"

    # ------------------------------------------------------------------ 校验
    @field_validator("public_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        parsed = urlsplit(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("PUBLIC_BASE_URL 必须是完整的 http(s)://host[:port][/path]")
        if parsed.query or parsed.fragment:
            raise ValueError("PUBLIC_BASE_URL 不能带 query 或 fragment")
        return cleaned

    @field_validator("mail_mode")
    @classmethod
    def _check_mail_mode(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in {"log", "smtp"}:
            raise ValueError("MAIL_MODE 只允许 log 或 smtp")
        return mode

    @field_validator("mail_from")
    @classmethod
    def _check_mail_from(cls, value: str) -> str:
        cleaned = value.strip()
        try:
            validate_email(cleaned, check_deliverability=False)
        except EmailNotValidError as exc:
            raise ValueError(f"MAIL_FROM 不是合法邮箱：{exc}") from exc
        return cleaned

    @field_validator("email_internal_token")
    @classmethod
    def _check_internal_token(cls, value: str) -> str:
        token = value.strip()
        if token and len(token) < 32:
            raise ValueError("EMAIL_INTERNAL_TOKEN 至少 32 个字符")
        return token

    @field_validator("callback_signing_secret")
    @classmethod
    def _check_callback_secret(cls, value: str) -> str:
        secret = value.strip()
        if secret and len(secret) < 32:
            raise ValueError("CALLBACK_SIGNING_SECRET 至少 32 个字符")
        return secret

    @field_validator("verification_ttl_seconds")
    @classmethod
    def _check_ttl(cls, value: int) -> int:
        if not 60 <= value <= 86400:
            raise ValueError("VERIFICATION_TTL_SECONDS 必须在 60~86400 之间")
        return value

    @field_validator("max_active_per_email", "confirm_rate_limit_per_minute")
    @classmethod
    def _check_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("该配置必须 >= 1")
        return value

    @field_validator("create_cooldown_seconds", "retention_days")
    @classmethod
    def _check_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("该配置不能为负数")
        return value

    @field_validator("callback_retry_schedule")
    @classmethod
    def _check_schedule(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            raise ValueError("CALLBACK_RETRY_SCHEDULE 不能为空")
        try:
            seconds = [int(part) for part in parts]
        except ValueError as exc:
            raise ValueError("CALLBACK_RETRY_SCHEDULE 必须是逗号分隔的秒数") from exc
        if seconds[0] != 0:
            raise ValueError("CALLBACK_RETRY_SCHEDULE 第一项必须是 0")
        if any(second < 0 for second in seconds):
            raise ValueError("CALLBACK_RETRY_SCHEDULE 不能包含负数")
        return ",".join(str(second) for second in seconds)

    @field_validator("callback_max_attempts")
    @classmethod
    def _check_attempts(cls, value: int) -> int:
        if not 1 <= value <= 20:
            raise ValueError("CALLBACK_MAX_ATTEMPTS 必须在 1~20 之间")
        return value

    @field_validator("callback_timeout_seconds", "worker_interval_seconds")
    @classmethod
    def _check_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("超时/间隔必须大于 0")
        return value

    @model_validator(mode="after")
    def _check_smtp_config(self) -> "Settings":
        if self.mail_mode == "smtp" and not self.smtp_host.strip():
            raise ValueError("MAIL_MODE=smtp 时必须配置 SMTP_HOST")
        return self

    # ------------------------------------------------------------ 派生配置
    @property
    def callback_allowed_host_set(self) -> set[str]:
        return {
            host.strip().lower()
            for host in self.callback_allowed_hosts.split(",")
            if host.strip()
        }

    @property
    def allowed_template_list(self) -> list[str]:
        return [
            name.strip()
            for name in self.allowed_templates.split(",")
            if name.strip()
        ]

    @property
    def retry_schedule_seconds(self) -> list[int]:
        return [int(part) for part in self.callback_retry_schedule.split(",")]

    def confirm_url(self, token: str) -> str:
        """确认链接的对外地址；前缀由 PUBLIC_BASE_URL 决定。"""
        return f"{self.public_base_url}/v1/confirm?token={quote(token, safe='')}"

    def base_path(self) -> str:
        return urlsplit(self.public_base_url).path.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：清掉缓存，让下一次 get_settings() 重新读环境变量。"""
    get_settings.cache_clear()
