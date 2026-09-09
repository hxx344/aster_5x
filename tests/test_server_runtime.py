import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


class ServerRuntimeTests(unittest.TestCase):
    def test_occupied_port_does_not_start_or_create_strategy_database(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            result = subprocess.run([sys.executable, "-m", "trading.server", "--demo", "--port", str(port)], cwd=ROOT,
                                    env={**os.environ, "ASTER_TRADING_RUNTIME": directory}, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(directory) / "trading.sqlite3").exists())

    @unittest.skipUnless(sys.platform == "linux", "SIGTERM lifecycle is a Linux service check")
    def test_demo_http_startup_and_graceful_shutdown(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as selected:
            selected.bind(("127.0.0.1", 0))
            port = selected.getsockname()[1]
            selected.close()
            public = Path(directory) / "public"
            public.mkdir()
            (public / "index.html").write_text("<html>Aster runtime test</html>")
            process = subprocess.Popen([sys.executable, "-m", "trading.server", "--demo", "--port", str(port)], cwd=ROOT,
                                       env={**os.environ, "ASTER_TRADING_RUNTIME": directory, "ASTER_DASHBOARD_DIR": str(public)},
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                            if json.load(response)["status"] == "ok":
                                break
                    except OSError:
                        pass
                    if time.monotonic() > deadline or process.poll() is not None:
                        self.fail("Demo server did not become healthy")
                    time.sleep(.1)
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    self.assertIn(b"Aster runtime test", response.read())
                with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as response:
                    data = json.load(response)
                self.assertTrue(data["demo"])
                self.assertEqual(data["accounts"][0]["mode"], "paper")
                process.send_signal(signal.SIGTERM)
                # Uvicorn re-raises the original signal after completing shutdown.
                self.assertIn(process.wait(timeout=15), (0, -signal.SIGTERM))
                self.assertIn(b"Application shutdown complete", process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
