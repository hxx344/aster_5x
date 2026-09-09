"""Authenticated local HTTP API and static dashboard, deployed behind HTTPS."""
import argparse
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import socket
import threading
from time import monotonic
from typing import Literal

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from starlette.datastructures import MutableHeaders

from .engine import Engine
from .models import TradingError
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY_BYTES = 16384
BODY_TIMEOUT_SECONDS = 10
SESSION_SECONDS = 43200
MAX_SESSIONS = 1024
LOGIN_WINDOW_SECONDS = 300
MAX_LOGIN_CLIENTS = 1024


class RequestSecurityMiddleware:
    """Bound actual request bytes before parsing, including chunked requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "same-origin"
                if scope["path"].startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        lengths = [value for key, value in scope["headers"] if key.lower() == b"content-length"]
        declared = None
        if lengths:
            value = lengths[0]
            # Bound digit conversion too: huge integers and duplicate lengths are invalid.
            if len(lengths) != 1 or not value.isdigit() or len(value) > 20:
                return await Response(status_code=413)(scope, receive, secure_send)
            declared = int(value)
            if declared > MAX_BODY_BYTES:
                return await Response(status_code=413)(scope, receive, secure_send)

        body = bytearray()
        try:
            with anyio.fail_after(BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        return await Response(status_code=413)(scope, receive, secure_send)
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await Response(status_code=408)(scope, receive, secure_send)
        if declared is not None and declared != len(body):
            return await Response(status_code=400)(scope, receive, secure_send)

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, secure_send)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class NewAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    name: str = Field(min_length=1, max_length=50)
    env_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,40}$")
    mode: Literal["paper", "live"]


class PolicyEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    threshold: str | None = Field(default=None, min_length=1, max_length=40)
    order_notional: str | None = Field(default=None, min_length=1, max_length=40)
    margin_limit: str | None = Field(default=None, min_length=1, max_length=128)
    min_open_leverage: StrictInt | None = Field(default=None, ge=1, le=125)

    @model_validator(mode="before")
    @classmethod
    def require_present_values(cls, values):
        if not isinstance(values, dict) or not values or any(value is None for value in values.values()):
            raise ValueError("请提供非空的策略配置字段")
        return values


def create_app(engine=None, *, demo=False, start_engine=True):
    if engine is None:
        runtime = Path(os.environ.get("ASTER_TRADING_RUNTIME", ROOT / "runtime" / "trading"))
        engine = Engine(Store(runtime / "trading.sqlite3"), demo=demo)
    password = os.environ.get("ASTER_DASHBOARD_PASSWORD", "")
    password_digest = hashlib.sha256(password.encode()).digest()
    failures, fail_lock = OrderedDict(), threading.Lock()
    sessions, session_lock = OrderedDict(), threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if start_engine:
            engine.start()
        try:
            yield
        finally:
            if start_engine:
                engine.stop()

    app = FastAPI(title="Aster Account Desk", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.engine = engine
    app.add_middleware(RequestSecurityMiddleware)

    def session_token(previous):
        # Server-side expiry makes logout and login rotation revoke copied cookies too.
        token = secrets.token_urlsafe(32)
        with session_lock:
            now = monotonic()
            while sessions and next(iter(sessions.values())) <= now:
                sessions.popitem(last=False)
            sessions.pop(previous, None)
            while len(sessions) >= MAX_SESSIONS:
                sessions.popitem(last=False)
            sessions[token] = now + SESSION_SECONDS
        return token

    async def authenticated(request: Request):
        if engine.demo:
            return
        token = request.cookies.get("aster_session", "")
        with session_lock:
            expires = sessions.get(token)
            if expires is None or expires <= monotonic():
                sessions.pop(token, None)
                raise HTTPException(401, "请登录交易管理")

    async def origin_check(request: Request):
        expected = os.environ.get("ASTER_PUBLIC_ORIGIN", str(request.base_url).rstrip("/"))
        allowed = {expected}
        if engine.demo:
            allowed.update({"http://127.0.0.1:3000", "http://localhost:3000"})
        if request.headers.get("origin") not in allowed:
            raise HTTPException(403, "请求来源不匹配")

    @app.exception_handler(TradingError)
    async def trading_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        from fastapi.responses import JSONResponse
        # Do not echo arbitrary submitted fields or passwords in validation errors.
        return JSONResponse({"detail": "输入字段或格式无效"}, status_code=422)

    @app.get("/api/health")
    async def health(response: Response):
        thread = engine.thread if start_engine else None
        if engine.shutdown.is_set() or (start_engine and (thread is None or not thread.is_alive())):
            response.status_code = 503
            return {"status": "unavailable", "demo": engine.demo}
        return {"status": "ok" if engine.ready else "starting", "demo": engine.demo}

    @app.post("/api/login", dependencies=[Depends(origin_check)])
    def login(body: Login, request: Request, response: Response):
        if len(password) < 16:
            raise HTTPException(503, "请先在服务器设置至少 16 字符的 ASTER_DASHBOARD_PASSWORD")
        client = request.client.host if request.client else "unknown"
        with fail_lock:
            now = monotonic()
            # Ordered by last failure; expired clients are removed once, not scanned
            # on every attempt. Each remaining client holds at most five timestamps.
            while failures and next(iter(failures.values()))[-1] <= now - LOGIN_WINDOW_SECONDS:
                failures.popitem(last=False)
            attempts = failures.get(client, deque())
            while attempts and attempts[0] <= now - LOGIN_WINDOW_SECONDS:
                attempts.popleft()
            if len(attempts) >= 5:
                raise HTTPException(429, "登录失败次数过多，请 5 分钟后重试")
            valid = hmac.compare_digest(password_digest, hashlib.sha256(body.password.encode()).digest())
            if not valid:
                if client not in failures and len(failures) >= MAX_LOGIN_CLIENTS:
                    raise HTTPException(429, "登录请求过多，请稍后重试")
                attempts.append(now)
                failures[client] = attempts
                failures.move_to_end(client)
                raise HTTPException(401, "访问密码不正确")
            failures.pop(client, None)
        response.set_cookie("aster_session", session_token(request.cookies.get("aster_session", "")), httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", max_age=SESSION_SECONDS, path="/")
        return {"ok": True}

    @app.post("/api/logout", dependencies=[Depends(origin_check)])
    def logout(request: Request, response: Response):
        with session_lock:
            sessions.pop(request.cookies.get("aster_session", ""), None)
        response.delete_cookie("aster_session", path="/", secure=request.url.scheme == "https", httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/state", dependencies=[Depends(authenticated)])
    def state():
        return engine.state()

    write_dependencies = [Depends(authenticated), Depends(origin_check)]

    @app.post("/api/accounts", dependencies=write_dependencies)
    def add_account(body: NewAccount):
        engine.add_account(body.model_dump())
        return {"ok": True}

    @app.patch("/api/accounts/{account_id}", dependencies=write_dependencies)
    def configure(account_id: str, body: PolicyEdit):
        engine.configure(account_id, body.model_dump(exclude_unset=True))
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/enable", dependencies=write_dependencies)
    def enable(account_id: str):
        engine.enable(account_id, True)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/pause", dependencies=write_dependencies)
    def pause(account_id: str):
        engine.enable(account_id, False)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/retry", dependencies=write_dependencies)
    def retry(account_id: str):
        engine.retry(account_id)
        return {"ok": True}

    output = Path(os.environ.get("ASTER_DASHBOARD_DIR", ROOT / "dashboard" / "dist" / "client"))
    if output.exists():
        app.mount("/", StaticFiles(directory=output, html=True), name="dashboard")
    else:
        @app.get("/")
        def missing_dashboard():
            raise HTTPException(503, "前端尚未构建，请完成安装或执行前端构建")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Isolated paper-only demonstration")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    if args.demo and args.host not in ("127.0.0.1", "localhost"):
        parser.error("Demo mode is restricted to loopback")
    if args.demo:
        os.environ.setdefault("ASTER_TRADING_RUNTIME", str(ROOT / "runtime" / "demo"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Signed query URLs must not be emitted by an HTTP client's diagnostic logger.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    import uvicorn
    # Reserve the listening address before any persisted strategy can resume.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Rebind after a Linux service restart even if accepted connections remain
        # in TIME_WAIT. Windows SO_REUSEADDR can allow taking an occupied address.
        if os.name != "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.host, args.port))
        config = uvicorn.Config(create_app(demo=args.demo), host=args.host, port=args.port,
                                proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False)
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()
