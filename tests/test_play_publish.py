"""Tests for scripts/play_publish.py. Standard library only:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import http.server
import importlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import play_publish  # noqa: E402

ANDROID_NS = "http://schemas.android.com/apk/res/android"


# --------------------------------------------------------------------------
# Building manifests the way aapt2 writes them
# --------------------------------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode()
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def proto_manifest(package: str, version_code: int = 7) -> bytes:
    """An XmlNode for <manifest package=... android:versionCode=...>."""
    ns_decl = _field(1, "android") + _field(2, ANDROID_NS)
    version_attr = _field(1, ANDROID_NS) + _field(2, "versionCode") + _field(3, str(version_code))
    # android:package in the android namespace must not be mistaken for it.
    decoy = _field(1, ANDROID_NS) + _field(2, "package") + _field(3, "decoy.wrong")
    package_attr = _field(2, "package") + _field(3, package)
    child = _field(1, _field(3, "application"))
    element = (_field(1, ns_decl) + _field(3, "manifest") + _field(4, version_attr)
               + _field(4, decoy) + _field(4, package_attr) + _field(5, child))
    return _field(1, element)


def _string_pool(strings: list[str], utf8: bool) -> bytes:
    data = bytearray()
    offsets = []
    for s in strings:
        offsets.append(len(data))
        if utf8:
            raw = s.encode("utf-8")
            for length in (len(s), len(raw)):
                data += bytes([length]) if length < 0x80 else bytes([0x80 | (length >> 8), length & 0xFF])
            data += raw + b"\0"
        else:
            data += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0"
    while len(data) % 4:
        data += b"\0"
    header_size = 28
    strings_start = header_size + 4 * len(strings)
    size = strings_start + len(data)
    return (struct.pack("<HHIIIIII", 0x0001, header_size, size, len(strings), 0,
                        (1 << 8) if utf8 else 0, strings_start, 0)
            + struct.pack(f"<{len(strings)}I", *offsets) + bytes(data))


def binary_manifest(package: str, utf8: bool = False, typed_only: bool = False) -> bytes:
    strings = ["versionCode", "package", "manifest", ANDROID_NS, "android", "decoy.wrong", package]
    idx = {s: i for i, s in enumerate(strings)}
    none = 0xFFFFFFFF

    def attr(ns, name, raw, data_type, data):
        return struct.pack("<IIIHBBI", ns, name, raw, 8, 0, data_type, data)

    attrs = [
        attr(idx[ANDROID_NS], idx["versionCode"], none, 0x10, 7),
        attr(idx[ANDROID_NS], idx["package"], idx["decoy.wrong"], 0x03, idx["decoy.wrong"]),
        attr(none, idx["package"], none if typed_only else idx[package], 0x03, idx[package]),
    ]
    ext = struct.pack("<IIHHHHHH", none, idx["manifest"], 20, 20, len(attrs), 0, 0, 0)
    body = struct.pack("<II", 1, none) + ext + b"".join(attrs)
    start_element = struct.pack("<HHI", 0x0102, 16, 8 + len(body)) + body
    ns_body = struct.pack("<IIII", 1, none, idx["android"], idx[ANDROID_NS])
    start_ns = struct.pack("<HHI", 0x0100, 16, 8 + len(ns_body)) + ns_body
    chunks = _string_pool(strings, utf8) + start_ns + start_element
    return struct.pack("<HHI", 0x0003, 8, 8 + len(chunks)) + chunks


def write_zip(path: str, entries: dict) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_bundle_package(self):
        aab = write_zip(os.path.join(self.tmp, "app.aab"), {
            "base/manifest/AndroidManifest.xml": proto_manifest("com.example.app.dev"),
            "BundleConfig.pb": b""})
        self.assertEqual(play_publish.package_from_artifact(aab), "com.example.app.dev")

    def test_apk_package_utf16(self):
        apk = write_zip(os.path.join(self.tmp, "app.apk"),
                        {"AndroidManifest.xml": binary_manifest("com.example.app")})
        self.assertEqual(play_publish.package_from_artifact(apk), "com.example.app")

    def test_apk_package_utf8(self):
        apk = write_zip(os.path.join(self.tmp, "app.apk"),
                        {"AndroidManifest.xml": binary_manifest("com.example.ütf8", utf8=True)})
        self.assertEqual(play_publish.package_from_artifact(apk), "com.example.ütf8")

    def test_apk_package_typed_value_only(self):
        apk = write_zip(os.path.join(self.tmp, "app.apk"),
                        {"AndroidManifest.xml": binary_manifest("com.example.typed", typed_only=True)})
        self.assertEqual(play_publish.package_from_artifact(apk), "com.example.typed")

    def test_no_manifest(self):
        path = write_zip(os.path.join(self.tmp, "x.aab"), {"other": b""})
        with self.assertRaises(ValueError):
            play_publish.package_from_artifact(path)


class ReleaseTests(unittest.TestCase):
    def build(self, notes="", **values):
        environ = {f"PLAY_{k.upper()}": v for k, v in values.items()}
        with mock.patch.dict(os.environ, environ, clear=False):
            for key in ("PLAY_STATUS", "PLAY_USER_FRACTION", "PLAY_RELEASE_NAME",
                        "PLAY_IN_APP_UPDATE_PRIORITY", "PLAY_RELEASE_NOTES_LANGUAGE"):
                if key not in environ:
                    os.environ.pop(key, None)
            return play_publish.build_release(["12"], notes)

    def test_defaults(self):
        self.assertEqual(self.build(), {"versionCodes": ["12"], "status": "completed"})

    def test_notes_and_language(self):
        release = self.build("Fixes", release_notes_language="es-419")
        self.assertEqual(release["releaseNotes"], [{"language": "es-419", "text": "Fixes"}])

    def test_staged_rollout(self):
        release = self.build(status="inProgress", user_fraction="0.25", in_app_update_priority="3")
        self.assertEqual(release["userFraction"], 0.25)
        self.assertEqual(release["inAppUpdatePriority"], 3)

    def test_invalid(self):
        for values in ({"status": "live"},
                       {"status": "inProgress"},
                       {"status": "completed", "user_fraction": "0.5"},
                       {"status": "inProgress", "user_fraction": "1"},
                       {"status": "inProgress", "user_fraction": "ten"},
                       {"in_app_update_priority": "6"}):
            with self.subTest(values=values), self.assertRaises(play_publish.ActionError):
                self.build(**values)

    def test_long_notes_are_shortened(self):
        with mock.patch.dict(os.environ, {"PLAY_RELEASE_NOTES": "x" * 700, "PLAY_RELEASE_NOTES_FILE": ""}):
            notes = play_publish.read_release_notes()
        self.assertEqual(len(notes), play_publish.RELEASE_NOTES_LIMIT)

    def test_hint_for_draft_app(self):
        err = play_publish.PlayApiError("Updating track", 400,
                                        "Only releases with status draft may be created on draft app.")
        self.assertIn("status: draft", str(err))


# --------------------------------------------------------------------------
# End to end, against a local stand-in for Google
# --------------------------------------------------------------------------


class FakeGoogle(http.server.BaseHTTPRequestHandler):
    """Records every request; answers like the token endpoint and Play."""

    calls: list = []
    public_key_path = ""
    fail_on: str = ""

    def log_message(self, *args):
        pass

    def _reply(self, status=200, body=None, headers=None):
        payload = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urllib.parse.urlsplit(self.path)
        self.calls.append((self.command, path.path, path.query, body, dict(self.headers)))
        port = self.server.server_address[1]
        base = "/androidpublisher/v3/applications/com.example.app"
        upload = "/upload" + base

        if path.path == "/token":
            form = urllib.parse.parse_qs(body.decode())
            header, claims, signature = form["assertion"][0].split(".")
            pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
            with tempfile.NamedTemporaryFile(delete=False) as sig:
                sig.write(base64.urlsafe_b64decode(pad(signature)))
            verified = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", self.public_key_path, "-signature", sig.name],
                input=f"{header}.{claims}".encode(), capture_output=True)
            os.unlink(sig.name)
            claims = json.loads(base64.urlsafe_b64decode(pad(claims)))
            ok = verified.returncode == 0 and claims["scope"] == play_publish.SCOPE
            return self._reply(200 if ok else 400, {"access_token": "tok-123"} if ok else {"error": "bad"})

        if self.headers.get("Authorization") != "Bearer tok-123":
            return self._reply(401, {"error": {"message": "unauthenticated"}})
        if self.fail_on and self.fail_on in path.path:
            return self._reply(400, {"error": {"code": 400, "message":
                                               "Only releases with status draft may be created on draft app."}})
        if self.command == "POST" and path.path == base + "/edits":
            return self._reply(200, {"id": "edit-1"})
        if self.command == "POST" and path.path.startswith(upload) and "resumable" in path.query:
            return self._reply(200, {}, {"Location": f"http://127.0.0.1:{port}/session{path.path}"})
        if self.command == "PUT" and path.path.startswith("/session"):
            if path.path.endswith("/bundles"):
                return self._reply(200, {"versionCode": 42, "sha256": "abc"})
            return self._reply(200, {})
        if self.command == "PUT" and path.path == base + "/edits/edit-1/tracks/beta":
            return self._reply(200, json.loads(body))
        if self.command == "POST" and path.path == base + "/edits/edit-1:commit":
            return self._reply(200, {"id": "edit-1"})
        if self.command == "DELETE" and path.path == base + "/edits/edit-1":
            return self._reply(200, {})
        return self._reply(404, {"error": {"message": f"unexpected {self.command} {path.path}"}})

    do_GET = do_POST = do_PUT = do_DELETE = _handle


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        key = os.path.join(cls.tmp, "key.pem")
        pub = os.path.join(cls.tmp, "pub.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", key], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
        with open(key) as fh:
            cls.private_key = fh.read()
        FakeGoogle.public_key_path = pub
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.root = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeGoogle.calls = []
        FakeGoogle.fail_on = ""
        self.aab = write_zip(os.path.join(self.tmp, "app.aab"),
                             {"base/manifest/AndroidManifest.xml": proto_manifest("com.example.app")})
        self.mapping = os.path.join(self.tmp, "mapping.txt")
        with open(self.mapping, "w") as fh:
            fh.write("a -> b\n")
        self.output = os.path.join(self.tmp, "output")
        open(self.output, "w").close()
        self.creds = json.dumps({"type": "service_account", "client_email": "ci@example.iam.gserviceaccount.com",
                                 "private_key": self.private_key, "private_key_id": "k1",
                                 "token_uri": self.root + "/token"})

    def run_action(self, **overrides):
        environ = {
            "PLAY_ARTIFACT": self.aab,
            "PLAY_CREDENTIALS": self.creds,
            "PLAY_TRACK": "beta",
            "PLAY_RELEASE_NOTES": "Line one\nLine `two` $(nope)\n\nVersion: 1.2.3+42",
            "PLAY_MAPPING_FILE": self.mapping,
            "PLAY_API_ROOT": self.root,
            "GITHUB_OUTPUT": self.output,
            "RUNNER_TEMP": self.tmp,
        }
        environ.update(overrides)
        with mock.patch.dict(os.environ, environ, clear=True):
            importlib.reload(play_publish)
            try:
                return play_publish.main()
            finally:
                os.environ.pop("PLAY_API_ROOT", None)
                importlib.reload(play_publish)

    def test_publishes_bundle(self):
        self.assertEqual(self.run_action(), 0)
        steps = [(m, p.split("/applications/com.example.app")[-1] if "applications" in p else p)
                 for m, p, *_ in FakeGoogle.calls]
        self.assertEqual(steps, [
            ("POST", "/token"),
            ("POST", "/edits"),
            ("POST", "/edits/edit-1/bundles"),
            ("PUT", "/edits/edit-1/bundles"),
            ("POST", "/edits/edit-1/apks/42/deobfuscationFiles/proguard"),
            ("PUT", "/edits/edit-1/apks/42/deobfuscationFiles/proguard"),
            ("PUT", "/edits/edit-1/tracks/beta"),
            ("POST", "/edits/edit-1:commit"),
        ])
        uploaded = FakeGoogle.calls[3][3]
        with open(self.aab, "rb") as fh:
            self.assertEqual(uploaded, fh.read())
        track = json.loads(FakeGoogle.calls[6][3])
        self.assertEqual(track["track"], "beta")
        self.assertEqual(track["releases"][0]["versionCodes"], ["42"])
        self.assertEqual(track["releases"][0]["releaseNotes"][0]["text"],
                         "Line one\nLine `two` $(nope)\n\nVersion: 1.2.3+42")
        self.assertEqual(FakeGoogle.calls[7][2], "")
        with open(self.output) as fh:
            out = fh.read()
        self.assertIn("version-code<<__PLAY_EOF__\n42\n", out)
        self.assertIn("package-name<<__PLAY_EOF__\ncom.example.app\n", out)

    def test_changes_not_sent_for_review(self):
        self.assertEqual(self.run_action(PLAY_CHANGES_NOT_SENT_FOR_REVIEW="true"), 0)
        self.assertEqual(FakeGoogle.calls[-1][2], "changesNotSentForReview=true")

    def test_failure_discards_edit(self):
        FakeGoogle.fail_on = "/tracks/"
        self.assertEqual(self.run_action(), 1)
        self.assertEqual(FakeGoogle.calls[-1][0], "DELETE")
        self.assertFalse(any(p.endswith(":commit") for _, p, *_ in FakeGoogle.calls))

    def test_package_mismatch_fails_before_any_request(self):
        self.assertEqual(self.run_action(PLAY_PACKAGE_NAME="com.example.other"), 1)
        self.assertEqual(FakeGoogle.calls, [])

    def test_bad_credentials_fail_before_any_request(self):
        for creds in ("not json", json.dumps({"type": "authorized_user"}), ""):
            with self.subTest(creds=creds[:20]):
                self.assertEqual(self.run_action(PLAY_CREDENTIALS=creds), 1)
                self.assertEqual(FakeGoogle.calls, [])


if __name__ == "__main__":
    unittest.main()
