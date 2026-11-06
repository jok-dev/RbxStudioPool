"""Offline tests for cloudplace: a local HTTP server stands in for Asset Delivery and the CDN.

    python -m unittest test_cloudplace
"""

import gzip
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import cloudplace

PLACE = b"<roblox!" + b"\x00" * 64


class Fake(BaseHTTPRequestHandler):
    version = "v1"
    status = 200
    downloads = 0
    keys = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        if self.path.startswith("/asset/"):
            cls.keys.append(self.headers.get("x-api-key"))
            if cls.status != 200:
                self.send_response(cls.status)
                self.end_headers()
                self.wfile.write(b'{"errors":[{"message":"denied"}]}')
                return
            port = self.server.server_address[1]
            body = json.dumps({"location": f"http://127.0.0.1:{port}/cdn/{cls.version}?sig={os.urandom(4).hex()}"})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())
        elif self.path.startswith("/cdn/"):
            cls.downloads += 1
            body = gzip.compress(PLACE + self.path.encode())
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class CloudPlaceTest(unittest.TestCase):
    def setUp(self):
        Fake.version, Fake.status, Fake.downloads, Fake.keys = "v1", 200, 0, []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self.patches = [mock.patch.object(cloudplace, "API", f"http://127.0.0.1:{port}/asset/{{}}"),
                        mock.patch.dict(os.environ, {cloudplace.ENV_KEY: "test-key"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_parse(self):
        self.assertEqual(cloudplace.parse("cloud:123"), "123")
        self.assertIsNone(cloudplace.parse("C:/x.rbxl"))
        with self.assertRaises(cloudplace.CloudPlaceError):
            cloudplace.parse("cloud:abc")

    def test_download_gunzips_and_caches_by_version(self):
        path, info = cloudplace.fetch("42", self.cache)
        self.assertEqual(info["cache"], "downloaded")
        self.assertTrue(path.read_bytes().startswith(PLACE))
        self.assertEqual(Fake.keys, ["test-key"])

        again, info = cloudplace.fetch("42", self.cache)  # new signature, same version
        self.assertEqual((again, info["cache"], Fake.downloads), (path, "hit", 1))

        Fake.version = "v2"
        new, info = cloudplace.fetch("42", self.cache)
        self.assertEqual((info["cache"], Fake.downloads), ("downloaded", 2))
        self.assertNotEqual(new, path)
        self.assertFalse(path.exists(), "old version pruned")

    def test_in_use_version_is_kept(self):
        old, _ = cloudplace.fetch("42", self.cache)
        Fake.version = "v2"
        cloudplace.fetch("42", self.cache, in_use={str(old)})
        self.assertTrue(old.exists())

    def test_denied_explains_scope_without_leaking_key(self):
        Fake.status = 403
        with self.assertRaises(cloudplace.CloudPlaceError) as cm:
            cloudplace.fetch("42", self.cache)
        self.assertIn("legacy-asset:manage", str(cm.exception))
        self.assertNotIn("test-key", str(cm.exception))

    def test_no_key(self):
        with mock.patch.dict(os.environ, {cloudplace.ENV_KEY: ""}), \
                mock.patch("keyring.get_password", return_value=None):
            with self.assertRaises(cloudplace.CloudPlaceError) as cm:
                cloudplace.fetch("42", self.cache)
        self.assertIn("cloud-key set", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
