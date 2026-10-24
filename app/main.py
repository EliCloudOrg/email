"""FastAPI 应用装配。

服务内路由**不带 /email 前缀**（网关剥离，见 README-deploy.md）：

    POST /v1/verify           GET /v1/verify/{id}
    GET  /v1/confirm          GET /v1/healthz
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings, get_settings
from .db import init_db
from .mailer import Mailer, build_mailer
from .routers import internal, public
from .worker import run_worker

logger = logging.getLogger("email")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.strip().upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app(settings: Settings | None = None, mailer: Mailer | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    init_db(settings)
    app_mailer: Mailer = mailer or build_mailer(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings: Settings = app.state.settings
        stop_event = threading.Event()
        http_client: httpx.Client | None = None
        worker_thread: threading.Thread | None = None
        logger.info(
            "email starting version=%s public_base_url=%s db=%s mail_mode=%s",
            __version__,
            settings.public_base_url,
            settings.database_url,
            settings.mail_mode,
        )
        if settings.worker_enabled:
            http_client = httpx.Client(
                timeout=settings.callback_timeout_seconds,
                follow_redirects=False,
            )
            worker_thread = threading.Thread(
                target=run_worker,
                args=(settings, stop_event, http_client),
                name="email-worker",
                daemon=True,
            )
            worker_thread.start()
        else:
            logger.info("worker disabled (WORKER_ENABLED=false)")
        try:
            yield
        finally:
            stop_event.set()
            if worker_thread is not None:
                worker_thread.join(timeout=5)
            if http_client is not None:
                http_client.close()
            logger.info("email stopped")

    app = FastAPI(
        title="EliCloud Email Verification",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.mailer = app_mailer

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled error path=%s", request.url.path)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        # 只记 path，不记 query string：confirm 的 token 在 query 里。
        log = logger.debug if request.url.path == "/v1/healthz" else logger.info
        log("%s %s -> %s %.1fms", request.method, request.url.path, response.status_code, elapsed_ms)
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            content = detail
        else:
            content = {"error": "http_error", "error_description": str(detail)}
        return JSONResponse(
            status_code=exc.status_code, content=content, headers=dict(exc.headers or {})
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {"loc": list(item.get("loc", ())), "msg": item.get("msg"), "type": item.get("type")}
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_request",
                "error_description": "请求参数不合法",
                "details": details,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception")
        return JSONResponse(
            status_code=500,
            content={"error": "server_error", "error_description": "服务内部错误"},
        )

    app.include_router(internal.router)
    app.include_router(public.router)
    return app


app = create_app()
