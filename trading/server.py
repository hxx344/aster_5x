"""Authenticated local HTTP API and static dashboard, deployed behind HTTPS."""
import argparse
from contextlib import asynccontextmanager
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .engine import Engine
from .models import TradingError
from .store import Store

ROOT = Path(__file__).resolve().parents[1]


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
    model_config = ConfigDict(extra="forbid")
    threshold: str = Field(min_length=1, max_length=40)
    order_notional: str = Field(min_length=1, max_length=40)


def create_app(engine=None, *, demo=False, start_engine=True):
    if engine is None:
        runtime = Path(os.environ.get("ASTER_TRADING_RUNTIME", ROOT / "runtime" / "trading"))
        engine = Engine(Store(runtime / "trading.sqlite3"), demo=demo)
    password = os.environ.get("ASTER_DASHBOARD_PASSWORD", "")
    secret = secrets.token_bytes(32)
    failures, fail_lock = {}, threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if start_engine:
            engine.start()
        yield
        if start_engine:
            engine.stop()

    app = FastAPI(title="Aster Account Desk", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.engine = engine

    def session_token():
        payload = f"{int(time.time()) + 43200}.{secrets.token_hex(16)}"
        return payload + "." + hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()

    def authenticated(request: Request):
        if engine.demo:
            return
        token = request.cookies.get("aster_session", "")
        try:
            payload, signature = token.rsplit(".", 1)
            expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected) or int(payload.split(".")[0]) < time.time():
                raise ValueError()
        except (ValueError, IndexError):
            raise HTTPException(401, "请登录交易管理") from None

    def origin_check(request: Request):
        expected = os.environ.get("ASTER_PUBLIC_ORIGIN", str(request.base_url).rstrip("/"))
        allowed = {expected}
        if engine.demo:
            allowed.update({"http://127.0.0.1:3000", "http://localhost:3000"})
        if request.headers.get("origin") not in allowed:
            raise HTTPException(403, "请求来源不匹配")

    @app.middleware("http")
    async def headers(request, call_next):
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 16384:
            return Response(status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

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
    def health():
        return {"status": "ok" if engine.ready else "starting", "demo": engine.demo}

    @app.post("/api/login", dependencies=[Depends(origin_check)])
    def login(body: Login, request: Request, response: Response):
        if len(password) < 16:
            raise HTTPException(503, "请先在服务器设置至少 16 字符的 ASTER_DASHBOARD_PASSWORD")
        client = request.client.host if request.client else "unknown"
        with fail_lock:
            now = time.time()
            for key in list(failures):
                failures[key] = [t for t in failures[key] if now - t < 300]
                if not failures[key]:
                    del failures[key]
            if len(failures.get(client, [])) >= 5:
                raise HTTPException(429, "登录失败次数过多，请 5 分钟后重试")
            valid = hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(body.password.encode()).digest())
            if not valid:
                failures.setdefault(client, []).append(now)
                raise HTTPException(401, "访问密码不正确")
            failures.pop(client, None)
        response.set_cookie("aster_session", session_token(), httponly=True, secure=request.url.scheme == "https", samesite="strict", max_age=43200, path="/")
        return {"ok": True}

    @app.post("/api/logout", dependencies=[Depends(origin_check)])
    def logout(response: Response):
        response.delete_cookie("aster_session", path="/")
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
        engine.configure(account_id, body.model_dump())
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
        listener.bind((args.host, args.port))
        config = uvicorn.Config(create_app(demo=args.demo), host=args.host, port=args.port,
                                proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False)
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()
