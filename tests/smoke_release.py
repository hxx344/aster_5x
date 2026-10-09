"""Exercise the published archive itself, without collectors, orders or external I/O."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "release-output").resolve()
    manifest = json.loads((output / "release-manifest.json").read_text(encoding="utf-8"))
    artifact = manifest["artifacts"]["linux-x64"]
    archive_path = output / artifact["file"]
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != artifact["sha256"]:
        raise ValueError("Runtime archive checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="checked-runtime-") as temporary:
        root = Path(temporary)
        with tarfile.open(archive_path) as archive:
            for entry in archive:
                name = PurePosixPath(entry.name)
                if not entry.isfile() or name.is_absolute() or ".." in name.parts:
                    raise ValueError("Runtime archives contain only ordinary relative files")
                path = root.joinpath(*name.parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(entry).read())
        if (root / ".release-commit").read_text().strip() != manifest["commit"]:
            raise ValueError("Archive provenance mismatch")
        environment = {**os.environ, "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
        repository = manifest["repository"]
        if repository.endswith("/variational-grid"):
            subprocess.run([sys.executable, "-B", "deploy_check.py"], cwd=root, env=environment, check=True)
            subprocess.run([sys.executable, "-B", "-m", "variational_grid", "demo"], cwd=root, env=environment, check=True, stdout=subprocess.DEVNULL)
        else:
            # ASGI requests do not open sockets; any unexpected exchange access fails.
            code = """import socket
original_connect = socket.socket.connect
def isolated_connect(connection, address):
    if connection.family == getattr(socket, 'AF_UNIX', None) or (isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1')):
        return original_connect(connection, address)
    raise AssertionError('Release smoke must not contact external services')
socket.socket.connect = isolated_connect
from fastapi.testclient import TestClient
"""
            if repository.endswith("/aster_5x"):
                environment.update(ASTER_TRADING_RUNTIME=str(root / "temporary-state"), ASTER_DASHBOARD_PASSWORD="release-smoke-password")
                code += """from trading.server import create_app
app = create_app(demo=True, start_engine=False)
client = TestClient(app)
health = client.get('/api/health')
assert health.status_code == 200 and health.json()['status'] in ('ok', 'starting') and health.json()['demo'] is True
response = client.get('/')
assert response.status_code == 200 and '<html' in response.text.lower()
from html.parser import HTMLParser
assets = []
class BrowserAssets(HTMLParser):
    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ('src', 'href') and value and value.startswith('/') and value.split('?', 1)[0].endswith(('.js', '.css')):
                assets.append(value)
BrowserAssets().feed(response.text)
assert assets, 'Built browser assets must be referenced'
for asset in assets:
    assert client.get(asset).status_code == 200, asset
client.close()
app.state.engine.dashboard_reports.close()
"""
            else:
                environment.update(STATE_FILE=str(root / "temporary-state.json"), TRADING_MODE="dry-run", LIVE_TRADING="false", AUTO_OPEN="false",
                                   DASHBOARD_USERNAME="smoke", DASHBOARD_PASSWORD="release-smoke-password", BYBIT_API_KEY="", BYBIT_API_SECRET="")
                code += """from app.main import app, engine
client = TestClient(app)
assert client.get('/api/health').status_code == 401
client.auth = ('smoke', 'release-smoke-password')
health = client.get('/api/health')
assert health.status_code == 200 and health.json()['trading_enabled'] is False
assert client.get('/').status_code == 200
assert client.get('/static/app.js').status_code == 200
client.close()
import asyncio
asyncio.run(engine.client.close())
"""
            subprocess.run([sys.executable, "-B", "-c", code], cwd=root, env=environment, check=True)
        print(f"Actual {repository} runtime archive passed its offline smoke check.")


if __name__ == "__main__":
    main()
