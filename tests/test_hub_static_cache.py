"""Only manifest-listed hashed public bundles may use immutable caching."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import Fixture
from trading.engine import Engine
from trading.server import create_app


class HubStaticCacheTests(TestCase):
    def test_only_built_hash_assets_are_publicly_immutable(self):
        f = Fixture()
        self.addCleanup(f.close)
        engine = Engine(f.store, market=f.market)
        self.addCleanup(engine.dashboard_reports.close)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"index.html": "<html>dashboard</html>", "404.html": "missing",
                     "_next/static/chunks/page-AbCd1234.js": "export default 1;",
                     "_next/static/css/index.AbCd1234.css": "body{}",
                     "_next/static/chunks/unlisted-AbCd1234.js": "export default 2;",
                     "_next/static/chunks/plain.js": "export default 3;"}
            for path, text in files.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            (root / ".vite").mkdir()
            (root / ".vite/manifest.json").write_text(json.dumps({
                "page": {"file": "_next/static/chunks/page-AbCd1234.js", "css": ["_next/static/css/index.AbCd1234.css"]},
                "plain": {"file": "_next/static/chunks/plain.js"},
                "html": {"file": "index.html"},
            }))
            with patch.dict("os.environ", {"ASTER_DASHBOARD_DIR": directory}), \
                 TestClient(create_app(engine, start_engine=False)) as client:
                for path in ("_next/static/chunks/page-AbCd1234.js", "_next/static/css/index.AbCd1234.css"):
                    response = client.get("/" + path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
                    conditional = client.get("/" + path, headers={"If-None-Match": response.headers["etag"]})
                    self.assertEqual(conditional.status_code, 304)
                    self.assertIn("immutable", conditional.headers["cache-control"])
                for path in ("/", "/_next/static/chunks/unlisted-AbCd1234.js", "/_next/static/chunks/plain.js", "/_next/static/chunks/missing-AbCd1234.js"):
                    self.assertNotIn("immutable", client.get(path).headers.get("cache-control", ""))
                api = client.get("/api/hub/summary?schemaVersion=2")
                self.assertEqual(api.status_code, 401)
                self.assertEqual(api.headers["cache-control"], "no-store")

    def test_missing_or_invalid_manifest_does_not_guess_cacheability(self):
        from trading.server import DashboardStaticFiles
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(DashboardStaticFiles(directory).immutable_paths, set())
            manifest = Path(directory) / ".vite/manifest.json"
            manifest.parent.mkdir()
            for text in ("not json", "[]", '{"bad":null}', '{"bad":{"file":42,"css":"private"}}'):
                manifest.write_text(text)
                self.assertEqual(DashboardStaticFiles(directory).immutable_paths, set())
