"""Local ASGI security regressions; no exchange, network, or credential access."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import socket
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import anyio
from fastapi import Request
from fastapi.testclient import TestClient
import httpx

from trading import server


PASSWORD = "server-security-test-password"


def app_fixture(*, start_engine=False):
    engine = SimpleNamespace(demo=False, store=Mock(), ready=True, state=Mock(return_value={"ok": True}),
                             start=Mock(), stop=Mock(), add_account=Mock(), configure=Mock(),
                             enable=Mock(), retry=Mock(), shutdown=threading.Event(), thread=None)
    with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": PASSWORD}, clear=True):
        app = server.create_app(engine, start_engine=start_engine)
    return app, engine


class SessionSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app, self.engine = app_fixture()
        self.client = self.new_client()

    def new_client(self, **kwargs):
        client = TestClient(self.app, **kwargs)
        self.addCleanup(client.close)
        client.headers["origin"] = str(client.base_url).rstrip("/")
        return client

    def login(self, client=None):
        client = client or self.client
        response = client.post("/api/login", json={"password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client.cookies.get("aster_session")

    def replay(self, token):
        return self.client.get("/api/state", headers={"cookie": f"aster_session={token}"})

    def test_logout_revokes_a_copied_cookie(self):
        token = self.login()
        self.assertEqual(self.client.post("/api/logout").status_code, 200)
        self.assertEqual(self.replay(token).status_code, 401)
        self.engine.state.assert_not_called()

    def test_relogin_rotates_and_revokes_previous_cookie(self):
        old = self.login()
        current = self.login()
        self.assertNotEqual(current, old)
        self.assertEqual(self.replay(old).status_code, 401)
        self.assertEqual(self.replay(current).status_code, 200)

    def test_logout_only_revokes_its_own_session(self):
        self.login()
        second = self.new_client()
        self.login(second)
        self.client.post("/api/logout")
        self.assertEqual(second.get("/api/state").status_code, 200)

    def test_session_expires_at_exact_monotonic_deadline(self):
        with patch("trading.server.monotonic", return_value=100):
            token = self.login()
        with patch("trading.server.monotonic", return_value=100 + server.SESSION_SECONDS - .001):
            self.assertEqual(self.replay(token).status_code, 200)
        with patch("trading.server.monotonic", return_value=100 + server.SESSION_SECONDS):
            self.assertEqual(self.replay(token).status_code, 401)

    def test_session_capacity_evicts_oldest_without_rejecting_new_login(self):
        with patch("trading.server.MAX_SESSIONS", 2):
            old = self.login()
            second = self.new_client()
            self.login(second)
            third = self.new_client()
            self.login(third)
            self.assertEqual(self.replay(old).status_code, 401)
            self.assertEqual(second.get("/api/state").status_code, 200)
            self.assertEqual(third.get("/api/state").status_code, 200)

    def test_unknown_tampered_and_non_ascii_cookies_are_unauthorized(self):
        token = self.login()
        for candidate in ("", ".", "a.b.c", token[:-1], token + "a", "x" * 8192):
            with self.subTest(candidate_length=len(candidate)):
                self.assertEqual(self.replay(candidate).status_code, 401)
        response = self.client.get("/api/state", headers={b"cookie": b"aster_session=1.nonce.\xff"})
        self.assertEqual(response.status_code, 401)

    def test_unknown_cookie_is_rejected_independent_of_monotonic_clock_origin(self):
        with patch("trading.server.monotonic", return_value=-100):
            self.assertEqual(self.replay("unknown").status_code, 401)

    def test_https_login_and_logout_cookies_have_secure_attributes(self):
        client = self.new_client(base_url="https://testserver")
        for path, body in (("/api/login", {"password": PASSWORD}), ("/api/logout", None)):
            with self.subTest(path=path):
                response = client.post(path, json=body)
                self.assertEqual(response.status_code, 200)
                for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
                    self.assertIn(attribute, response.headers["set-cookie"])

    def test_missing_null_and_foreign_origin_reject_every_mutation(self):
        token = self.login()
        operations = (("post", "/api/accounts", {"id": "test", "name": "test", "mode": "paper", "env_prefix": "ASTER_TEST"}),
                      ("patch", "/api/accounts/test", {"threshold": "10000", "order_notional": "1000"}),
                      ("post", "/api/accounts/test/enable", None),
                      ("post", "/api/accounts/test/pause", None),
                      ("post", "/api/accounts/test/retry", None),
                      ("post", "/api/logout", None),
                      ("post", "/api/login", {"password": PASSWORD}))
        for origin in (None, "null", "https://foreign.example", "http://testserver.attacker.example"):
            if origin is None:
                self.client.headers.pop("origin", None)
            else:
                self.client.headers["origin"] = origin
            for method, path, body in operations:
                with self.subTest(origin=origin, path=path):
                    response = self.client.request(method, path, json=body)
                    self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.replay(token).status_code, 200)
        for method in (self.engine.add_account, self.engine.configure, self.engine.enable, self.engine.retry):
            method.assert_not_called()

    def test_configured_origin_is_exact_and_ignores_forged_forwarded_header(self):
        self.login()
        with patch.dict(os.environ, {"ASTER_PUBLIC_ORIGIN": "https://desk.example"}):
            for candidate in ("http://testserver", "https://desk.example.attacker.example", "https://desk.example/"):
                result = self.client.post("/api/accounts/test/pause", headers={"origin": candidate,
                                          "x-forwarded-host": "desk.example"})
                self.assertEqual(result.status_code, 403)
            result = self.client.post("/api/accounts/test/pause", headers={"origin": "https://desk.example"})
            self.assertEqual(result.status_code, 200)
        self.engine.enable.assert_called_once_with("test", False)

    def test_login_failures_expire_at_exact_window_and_success_clears_them(self):
        with patch("trading.server.monotonic", return_value=100):
            for _ in range(5):
                self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
        with patch("trading.server.monotonic", return_value=399.999):
            self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 429)
        with patch("trading.server.monotonic", return_value=400):
            self.login()
            for _ in range(4):
                self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
            self.login()
            self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)

    def test_concurrent_wrong_password_attempts_cannot_bypass_limit(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(lambda _: self.client.post("/api/login", json={"password": "wrong"}).status_code,
                                     range(20)))
        self.assertEqual(statuses.count(401), 5)
        self.assertEqual(statuses.count(429), 15)

    def test_failure_cache_is_bounded_without_eviction_bypassing_existing_limit(self):
        second = self.new_client(client=("192.0.2.2", 5000))
        third = self.new_client(client=("192.0.2.3", 5000))
        with patch("trading.server.MAX_LOGIN_CLIENTS", 2), patch("trading.server.monotonic", return_value=100):
            for _ in range(5):
                self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
            self.assertEqual(second.post("/api/login", json={"password": "wrong"}).status_code, 401)
            self.assertEqual(third.post("/api/login", json={"password": "wrong"}).status_code, 429)
            self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 429)
            self.login(third)
        with patch("trading.server.MAX_LOGIN_CLIENTS", 2), patch("trading.server.monotonic", return_value=400):
            self.assertEqual(third.post("/api/login", json={"password": "wrong"}).status_code, 401)

    def test_validation_and_unexpected_errors_do_not_echo_submitted_secrets(self):
        secret = "unique-sensitive-value"
        response = self.client.post("/api/login", json={"password": secret, "extra": secret})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.login()
        self.engine.state.side_effect = RuntimeError(secret)
        quiet = self.new_client(raise_server_exceptions=False)
        self.login(quiet)
        response = quiet.get("/api/state")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(secret, response.text)


class RequestBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app, self.engine = app_fixture()

        @self.app.post("/api/echo")
        async def echo(request: Request):
            self.echo_body = await request.body()
            return {"length": len(self.echo_body)}
        # Put this test route ahead of the dashboard's catch-all static mount.
        self.app.router.routes.insert(0, self.app.router.routes.pop())

    async def raw_request(self, messages, *, headers=(), receive_override=None, path="/api/echo", method="POST"):
        pending, sent = iter(messages), []

        async def receive():
            return next(pending, {"type": "http.disconnect"})

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "headers": [(b"host", b"testserver"), *headers], "client": ("127.0.0.1", 5000),
                 "server": ("testserver", 80), "root_path": ""}
        await self.app(scope, receive_override or receive, send)
        starts = [message for message in sent if message["type"] == "http.response.start"]
        if not starts:
            return None, {}, b""
        return starts[0]["status"], dict(starts[0]["headers"]), b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body")

    async def test_actual_stream_limit_cannot_be_bypassed_by_missing_or_false_length(self):
        chunks = [{"type": "http.request", "body": b"x" * 8192, "more_body": True},
                  {"type": "http.request", "body": b"y" * 8193, "more_body": False}]
        for headers in ((), ((b"content-length", b"1"),), ((b"transfer-encoding", b"chunked"),)):
            with self.subTest(headers=headers):
                status, response_headers, _ = await self.raw_request(chunks, headers=headers)
                self.assertEqual(status, 413)
                self.assertEqual(response_headers[b"cache-control"], b"no-store")
                self.assertEqual(response_headers[b"x-content-type-options"], b"nosniff")

    async def test_exact_body_limit_is_accepted_and_replayed_intact(self):
        chunks = [{"type": "http.request", "body": b"x" * 8000, "more_body": True},
                  {"type": "http.request", "body": b"y" * (server.MAX_BODY_BYTES - 8000), "more_body": False}]
        status, _, body = await self.raw_request(chunks, headers=((b"content-length", str(server.MAX_BODY_BYTES).encode()),))
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"length":16384}')
        self.assertEqual(self.echo_body, b"x" * 8000 + b"y" * (server.MAX_BODY_BYTES - 8000))

    async def test_invalid_oversized_or_duplicate_lengths_reject_before_reading(self):
        async def unexpected_receive():
            self.fail("Invalid Content-Length should be rejected without reading the body")

        for values in ((b"9" * 10000,), (b"-1",), (b"abc",), (b"16385",), (b"0", b"0"), (b"1", b"2")):
            with self.subTest(lengths=[len(value) for value in values]):
                status, _, _ = await self.raw_request([], headers=tuple((b"content-length", value) for value in values),
                                                      receive_override=unexpected_receive)
                self.assertEqual(status, 413)

    async def test_declared_length_mismatch_is_rejected(self):
        for declared in (b"0", b"4"):
            status, _, _ = await self.raw_request([{"type": "http.request", "body": b"abc"}],
                                                  headers=((b"content-length", declared),))
            self.assertEqual(status, 400)

    async def test_disconnect_mid_body_never_reaches_business_logic(self):
        status, _, _ = await self.raw_request([{"type": "http.request", "body": b"partial", "more_body": True},
                                               {"type": "http.disconnect"}], path="/api/accounts/test/enable")
        self.assertIsNone(status)
        self.engine.enable.assert_not_called()

    async def test_stalled_body_returns_timeout_with_security_headers(self):
        async def stalled_receive():
            await asyncio.Event().wait()

        with patch("trading.server.BODY_TIMEOUT_SECONDS", .02):
            status, headers, _ = await self.raw_request([], receive_override=stalled_receive)
        self.assertEqual(status, 408)
        self.assertEqual(headers[b"cache-control"], b"no-store")

    async def test_health_remains_responsive_while_all_sync_workers_are_busy(self):
        started, release = threading.Event(), threading.Event()

        def blocked_state():
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test did not release blocked state")
            return {"ok": True}

        self.engine.state.side_effect = blocked_state
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver",
                                     headers={"origin": "http://testserver"}) as client:
            self.assertEqual((await client.post("/api/login", json={"password": PASSWORD})).status_code, 200)
            limiter.total_tokens = 1
            pending = asyncio.create_task(client.get("/api/state"))
            try:
                async def wait_until_started():
                    while not started.is_set():
                        await asyncio.sleep(.001)
                await asyncio.wait_for(wait_until_started(), timeout=2)
                health = await asyncio.wait_for(client.get("/api/health"), timeout=1)
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["status"], "ok")
            finally:
                release.set()
                await pending
                limiter.total_tokens = original_tokens

    async def test_lifespan_stops_engine_when_context_exits_with_an_error(self):
        app, engine = app_fixture(start_engine=True)
        with self.assertRaisesRegex(RuntimeError, "shutdown test"):
            async with app.router.lifespan_context(app):
                raise RuntimeError("shutdown test")
        engine.start.assert_called_once_with()
        engine.stop.assert_called_once_with()

    async def test_lifespan_does_not_start_or_stop_external_engine(self):
        async with self.app.router.lifespan_context(self.app):
            pass
        self.engine.start.assert_not_called()
        self.engine.stop.assert_not_called()


class HealthLifecycleTests(unittest.TestCase):
    def test_managed_engine_with_missing_or_dead_thread_is_unavailable_even_if_ready(self):
        for thread in (None, SimpleNamespace(is_alive=lambda: False)):
            with self.subTest(thread=thread):
                app, engine = app_fixture(start_engine=True)
                engine.thread = thread
                with TestClient(app) as client:
                    response = client.get("/api/health")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"status": "unavailable", "demo": False})
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_managed_live_thread_reports_starting_until_market_is_ready(self):
        app, engine = app_fixture(start_engine=True)
        engine.thread = SimpleNamespace(is_alive=lambda: True)
        with TestClient(app) as client:
            engine.ready = False
            response = client.get("/api/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "starting")
            engine.ready = True
            response = client.get("/api/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")

    def test_shutdown_signal_reports_unavailable_for_managed_and_external_engines(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                app, engine = app_fixture(start_engine=managed)
                engine.thread = SimpleNamespace(is_alive=lambda: True)
                engine.shutdown.set()
                with TestClient(app) as client:
                    response = client.get("/api/health")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["status"], "unavailable")

    def test_external_engine_needs_no_owned_thread_to_report_readiness(self):
        app, engine = app_fixture(start_engine=False)
        with TestClient(app) as client:
            for ready, expected in ((False, "starting"), (True, "ok")):
                engine.ready = ready
                response = client.get("/api/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], expected)
        engine.start.assert_not_called()
        engine.stop.assert_not_called()


class ListenerLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "TIME_WAIT restart is a Linux socket check")
    def test_main_can_immediately_rebind_after_active_connection_close(self):
        selected_port = None

        def accept_and_close(*, sockets):
            nonlocal selected_port
            listener = sockets[0]
            self.assertEqual(listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 1)
            listener.listen()
            selected_port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", selected_port), timeout=2) as peer:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    # The server actively closes first, placing its local port in
                    # TIME_WAIT; a normal client-initiated close would miss the bug.
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(peer.recv(1), b"")
                    peer.shutdown(socket.SHUT_WR)
                    self.assertEqual(connection.recv(1), b"")

        with patch("trading.server.create_app", return_value=object()) as create_app, \
                patch("uvicorn.Config"), patch("uvicorn.Server") as http_server:
            http_server.return_value.run.side_effect = accept_and_close
            with patch.object(sys, "argv", ["trading.server", "--port", "0"]):
                server.main()
            self.assertIsNotNone(selected_port)
            # Negative control proves this test actually produced a lingering
            # connection that requires reuse, rather than only closing a listener.
            with socket.socket() as without_reuse:
                with self.assertRaises(OSError):
                    without_reuse.bind(("127.0.0.1", selected_port))
            with patch.object(sys, "argv", ["trading.server", "--port", str(selected_port)]):
                server.main()
            self.assertEqual(create_app.call_count, 2)

    @unittest.skipUnless(sys.platform == "linux", "SO_REUSEADDR collision is a Linux socket check")
    def test_reuse_does_not_take_over_an_existing_live_listener(self):
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with patch("trading.server.create_app") as create_app, \
                    patch.object(sys, "argv", ["trading.server", "--port", str(occupied.getsockname()[1])]):
                with self.assertRaises(OSError):
                    server.main()
                create_app.assert_not_called()

    def test_windows_does_not_enable_address_reuse(self):
        with patch("trading.server.os.name", "nt"), patch("trading.server.socket.socket") as factory, \
                patch("trading.server.create_app", return_value=object()), patch("uvicorn.Config"), \
                patch("uvicorn.Server"), patch.object(sys, "argv", ["trading.server", "--port", "8765"]):
            server.main()
            factory.return_value.setsockopt.assert_not_called()
            factory.return_value.bind.assert_called_once_with(("127.0.0.1", 8765))
            factory.return_value.close.assert_called_once_with()
