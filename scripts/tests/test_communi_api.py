import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("communi_api", Path(__file__).parents[1] / "communi-api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class ClientTests(unittest.TestCase):
    def test_only_https_and_loopback_origins_are_accepted(self):
        for value in ["http://ckw.communi.ch", "https://user:secret@example.org", "https://example.org?token=secret", "https://example.org/#secret", "https://example.org/path", "file:///tmp/test"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.origin(value)
        self.assertEqual(api.origin("https://ckw.communi.ch/"), "https://ckw.communi.ch")
        self.assertEqual(api.origin("http://127.0.0.1:1234"), "http://127.0.0.1:1234")

    def test_private_token_file_and_environment(self):
        token = "communi_api_" + "a" * 43
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"COMMUNI_API_TOKEN": ""}):
            path = Path(temporary) / "key"
            path.write_text(token)
            path.chmod(0o600)
            self.assertEqual(api.read_token(path), token)
            if os.name != "nt":
                path.chmod(0o644)
                with self.assertRaises(ValueError): api.read_token(path)
            with patch.dict(os.environ, {"COMMUNI_API_TOKEN": token}):
                self.assertEqual(api.read_token(path), token)

    def test_http_roundtrip_upload_calculate_download_and_no_credential_redirect(self):
        calls = []
        content = b"timestamp,kwh\n2026-01-01,1.5\n"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                calls.append((self.path, self.headers.get("Authorization"), body))
                response = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response)
            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization"), None))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-receive-token")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(content)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
                source = Path(temporary) / "synthetic.csv"
                source.write_bytes(content)
                body, kind = api.multipart([source])
                api.request(base, "test-credential", "POST", "/upload", data=body, content_type=kind)
                self.assertIn(content, calls[-1][2])
                self.assertEqual(calls[-1][1], "Bearer test-credential")
                api.request(base, "test-credential", "POST", "/calculate", data={"parameters": {"tariffYear": 2026}})
                self.assertEqual(json.loads(calls[-1][2])["parameters"]["tariffYear"], 2026)
                output = Path(temporary) / "download.csv"
                api.request(base, "test-credential", "GET", "/download", output=output)
                self.assertEqual(output.read_bytes(), content)
                with self.assertRaises(FileExistsError): api.request(base, "test-credential", "GET", "/download", output=output)
                with self.assertRaisesRegex(ValueError, "HTTP 302"): api.request(base, "test-credential", "GET", "/redirect")
                self.assertFalse(any(path == "/must-not-receive-token" for path, _, _ in calls))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
