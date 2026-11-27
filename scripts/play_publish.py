#!/usr/bin/env python3
"""Upload an Android App Bundle or APK to a Google Play Console track.

Driven entirely by PLAY_* environment variables (see action.yaml), so no
input is ever pasted into a shell command. Standard library only: a
self-hosted runner needs python3 and openssl, nothing installed with pip.

The flow is the Play Developer API's edit transaction:

    insert edit -> upload artifact -> [upload mapping / symbols]
                -> update track -> commit

Nothing is visible in Play until the commit; any failure before it deletes
the edit, so a failed run leaves nothing half-done behind.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SCOPE = "https://www.googleapis.com/auth/androidpublisher"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
# Overridable only so the tests can point the script at a local fake.
API_ROOT = os.environ.get("PLAY_API_ROOT", "https://androidpublisher.googleapis.com").rstrip("/")

# Play refuses "What's new" text longer than this, per language.
RELEASE_NOTES_LIMIT = 500

STATUSES = ("completed", "inProgress", "draft", "halted")


class ActionError(Exception):
    """A failure with a message meant for the person reading the run."""


# --------------------------------------------------------------------------
# GitHub Actions plumbing
# --------------------------------------------------------------------------


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def gh_error(message: str) -> None:
    print(f"::error::{_escape_data(message)}", flush=True)


def gh_warning(message: str) -> None:
    print(f"::warning::{_escape_data(message)}", flush=True)


def gh_mask(value: str) -> None:
    if value:
        print(f"::add-mask::{value}", flush=True)


def gh_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<__PLAY_EOF__\n{value}\n__PLAY_EOF__\n")


def gh_summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# --------------------------------------------------------------------------
# Reading the package name out of the artifact
#
# The artifact is the authority on which app it belongs to, exactly as the
# IPA is for upload_build_to_testflight. Reading it rather than requiring an
# input means a flavored pipeline cannot pair one flavor's bundle with
# another flavor's package name, and when the input *is* given, a mismatch
# is caught here instead of as a confusing API error halfway through.
# --------------------------------------------------------------------------


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _proto_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for one protobuf message."""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            value, pos = buf[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield field, wire, value


def package_from_proto_manifest(data: bytes) -> str:
    """The package attribute of an AAB's protobuf AndroidManifest.xml.

    aapt2's Resources.proto: XmlNode{1: element}, XmlElement{3: name,
    4: attribute}, XmlAttribute{1: namespace_uri, 2: name, 3: value}.
    """
    for field, wire, node in _proto_fields(data):
        if field != 1 or wire != 2:
            continue
        name = ""
        attributes = []
        for efield, ewire, evalue in _proto_fields(node):
            if efield == 3 and ewire == 2:
                name = evalue.decode("utf-8")
            elif efield == 4 and ewire == 2:
                attributes.append(evalue)
        if name != "manifest":
            break
        for attribute in attributes:
            attr = {f: v for f, w, v in _proto_fields(attribute) if w == 2}
            if attr.get(1, b"") == b"" and attr.get(2) == b"package":
                return attr.get(3, b"").decode("utf-8")
        break
    raise ValueError("no package attribute on the <manifest> element")


def _axml_strings(data: bytes, offset: int) -> list[str]:
    (_, header_size, _, count, _, flags, strings_start, _) = struct.unpack_from(
        "<HHIIIIII", data, offset)
    utf8 = bool(flags & (1 << 8))
    offsets = struct.unpack_from(f"<{count}I", data, offset + header_size)
    base = offset + strings_start
    strings = []
    for rel in offsets:
        pos = base + rel
        if utf8:
            # Two lengths, each one or two bytes: UTF-16 units, then bytes.
            for _ in range(2):
                length = data[pos]
                pos += 1
                if length & 0x80:
                    length = ((length & 0x7F) << 8) | data[pos]
                    pos += 1
            strings.append(data[pos:pos + length].decode("utf-8", "replace"))
        else:
            length = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if length & 0x8000:
                length = ((length & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos)[0]
                pos += 2
            strings.append(data[pos:pos + length * 2].decode("utf-16-le", "replace"))
    return strings


def package_from_binary_manifest(data: bytes) -> str:
    """The package attribute of an APK's binary (AXML) AndroidManifest.xml."""
    file_type, file_header, _ = struct.unpack_from("<HHI", data, 0)
    if file_type != 0x0003:
        raise ValueError("not a binary XML document")
    strings: list[str] = []
    pos = file_header
    while pos + 8 <= len(data):
        chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, pos)
        if chunk_size < 8:
            raise ValueError("corrupt chunk")
        if chunk_type == 0x0001:  # string pool
            strings = _axml_strings(data, pos)
        elif chunk_type == 0x0102:  # start element
            ext = pos + header_size
            _, name_idx, attr_start, attr_size, attr_count = struct.unpack_from("<IIHHH", data, ext)
            if strings[name_idx] != "manifest":
                break
            for i in range(attr_count):
                apos = ext + attr_start + i * attr_size
                ns, aname, raw, _, _, data_type, value = struct.unpack_from("<IIIHBBI", data, apos)
                if ns == 0xFFFFFFFF and strings[aname] == "package":
                    if raw != 0xFFFFFFFF:
                        return strings[raw]
                    if data_type == 0x03:
                        return strings[value]
            break
        pos += chunk_size
    raise ValueError("no package attribute on the <manifest> element")


def package_from_artifact(path: str) -> str:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "base/manifest/AndroidManifest.xml" in names:
            return package_from_proto_manifest(archive.read("base/manifest/AndroidManifest.xml"))
        if "AndroidManifest.xml" in names:
            return package_from_binary_manifest(archive.read("AndroidManifest.xml"))
    raise ValueError("no AndroidManifest.xml in the archive")


# --------------------------------------------------------------------------
# Authentication: a service-account JWT exchanged for an access token
# --------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_rs256(message: bytes, private_key_pem: str) -> bytes:
    """RS256 with the openssl CLI, which every hosted runner image ships.

    The key is written to a private temporary directory, never to the
    workspace (a self-hosted runner keeps its workspace between runs), and is
    deleted as soon as the signature is made.
    """
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP") or None) as tmp:
        key_path = os.path.join(tmp, "key.pem")
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(private_key_pem)
        try:
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", key_path],
                input=message, capture_output=True, check=False)
        except FileNotFoundError:
            raise ActionError("openssl is required to sign the service-account token but was not found on PATH.")
        if result.returncode != 0:
            raise ActionError(
                "openssl could not sign with the service account's private_key: "
                + result.stderr.decode("utf-8", "replace").strip())
        return result.stdout


def load_credentials(raw: str, path: str) -> dict:
    if raw:
        source = "credentials"
        text = raw
    elif path:
        source = f"credentials-file '{path}'"
        if not os.path.isfile(path):
            raise ActionError(f"credentials-file '{path}' was not found.")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    else:
        raise ActionError("Provide either 'credentials' (the service account JSON) or 'credentials-file' (its path).")
    try:
        creds = json.loads(text)
    except json.JSONDecodeError as exc:
        # Never echo the text: it is a private key.
        raise ActionError(f"{source} is not valid JSON ({exc.msg} at line {exc.lineno}). "
                          "Pass the whole JSON key file of the service account, not base64 of it.")
    if not isinstance(creds, dict):
        raise ActionError(f"{source} is not a JSON object.")
    if creds.get("type") != "service_account":
        raise ActionError(f"{source} is not a service account key (type is '{creds.get('type')}'). "
                          "Create a JSON key for a service account in Google Cloud and pass that.")
    missing = [k for k in ("client_email", "private_key") if not creds.get(k)]
    if missing:
        raise ActionError(f"{source} has no {', '.join(missing)}.")
    return creds


def access_token(creds: dict) -> str:
    now = int(time.time())
    token_uri = creds.get("token_uri") or DEFAULT_TOKEN_URI
    header = {"alg": "RS256", "typ": "JWT"}
    if creds.get("private_key_id"):
        header["kid"] = creds["private_key_id"]
    claims = {
        "iss": creds["client_email"],
        "scope": SCOPE,
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    signature = sign_rs256(signing_input.encode("ascii"), creds["private_key"])
    assertion = signing_input + "." + _b64url(signature)

    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode()
    request = urllib.request.Request(token_uri, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            token = json.load(response).get("access_token", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ActionError(f"Google refused the service account's token request (HTTP {exc.code}): {detail}. "
                          "The key may have been deleted or disabled in Google Cloud.")
    if not token:
        raise ActionError("Google's token endpoint answered without an access_token.")
    gh_mask(token)
    return token


# --------------------------------------------------------------------------
# The Play Developer API
# --------------------------------------------------------------------------

# Matched against Google's error message to say what to do, not only what
# went wrong. Each of these has cost someone an afternoon at least once.
HINTS = (
    ("Only releases with status draft may be created on draft app",
     "This app has never been published, so Play accepts only status: draft. Set status to "
     "\"draft\", then roll the release out from the Play Console once; after that the other "
     "statuses work."),
    ("Package not found",
     "Play has no app with this package name that the service account can see. Create the app in "
     "the Play Console and upload its first build there by hand (the API cannot create an app), "
     "and invite the service account under Users and permissions."),
    ("The caller does not have permission",
     "The service account has no access to this app. In the Play Console, Users and permissions -> "
     "invite the service account's email and grant it release permissions for this app. A new "
     "invitation can take a while to start working."),
    ("has already been used",
     "Every upload needs a version code Play has never seen for this app, on any track. Raise the "
     "build number (the +N in pubspec.yaml) and build again."),
    ("Changes cannot be sent for review automatically",
     "Play requires these changes to be sent for review from the Play Console. Set "
     "changes-not-sent-for-review: true, then send them for review in the Console's Publishing "
     "overview."),
    ("Google Play Android Developer API has not been used",
     "Enable the Google Play Android Developer API in the service account's Google Cloud project."),
    ("Track names in request path and request body must match",
     "The track name is not one Play knows. Use internal, alpha, beta, production, or the exact "
     "name of a closed testing track."),
)


class PlayApiError(ActionError):
    def __init__(self, what: str, status: int, message: str):
        self.status = status
        self.api_message = message
        hint = next((h for needle, h in HINTS if needle.lower() in message.lower()), "")
        super().__init__(f"{what} failed (HTTP {status}): {message}" + (f"\n{hint}" if hint else ""))


class PlayClient:
    def __init__(self, token: str, package: str):
        self.token = token
        self.package = package
        self.base = f"{API_ROOT}/androidpublisher/v3/applications/{urllib.parse.quote(package, safe='')}"
        self.upload_base = (f"{API_ROOT}/upload/androidpublisher/v3/applications/"
                            f"{urllib.parse.quote(package, safe='')}")

    def _send(self, what: str, request: urllib.request.Request, retries: int):
        request.add_header("Authorization", f"Bearer {self.token}")
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=600) as response:
                    payload = response.read()
                    return response, (json.loads(payload) if payload.strip() else {})
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                try:
                    message = json.loads(body)["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    message = body.strip() or exc.reason
                transient = exc.code == 429 or exc.code >= 500
                if transient and attempt < retries:
                    delay = 5 * (2 ** attempt)
                    print(f"{what}: HTTP {exc.code}, retrying in {delay}s ({message})", flush=True)
                    time.sleep(delay)
                    continue
                raise PlayApiError(what, exc.code, message) from None
            except urllib.error.URLError as exc:
                if attempt < retries:
                    time.sleep(5 * (2 ** attempt))
                    continue
                raise ActionError(f"{what} failed: could not reach Google Play ({exc.reason}).") from None

    def call(self, what: str, method: str, path: str, body=None, query=None, retries: int = 3):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        return self._send(what, request, retries)[1]

    def upload(self, what: str, path: str, file_path: str, content_type: str) -> dict:
        """Google's resumable protocol: one request opens a session, one PUT sends the file.

        A simple (uploadType=media) request is capped well below what a
        bundle with native libraries for every ABI weighs, and the resumable
        session costs one extra round trip. The PUT is not retried: if it
        reached Play, a second one would be refused for reusing the version
        code, which reads as the wrong problem.
        """
        size = os.path.getsize(file_path)
        url = f"{self.upload_base}{path}?uploadType=resumable"
        start = urllib.request.Request(url, data=b"", method="POST", headers={
            "X-Upload-Content-Type": content_type,
            "X-Upload-Content-Length": str(size),
            "Content-Type": "application/json; charset=utf-8",
        })
        response, _ = self._send(f"{what} (opening the upload)", start, retries=3)
        location = response.headers.get("Location")
        if not location:
            raise ActionError(f"{what}: Play opened no upload session (no Location header).")
        with open(file_path, "rb") as fh:
            put = urllib.request.Request(location, data=fh, method="PUT", headers={
                "Content-Type": content_type,
                "Content-Length": str(size),
            })
            return self._send(what, put, retries=0)[1]


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def read_release_notes() -> str:
    text = os.environ.get("PLAY_RELEASE_NOTES", "")
    notes_file = env("PLAY_RELEASE_NOTES_FILE")
    if notes_file:
        if text.strip():
            raise ActionError("Pass release-notes or release-notes-file, not both.")
        if not os.path.isfile(notes_file):
            raise ActionError(f"release-notes-file '{notes_file}' was not found.")
        with open(notes_file, encoding="utf-8") as fh:
            text = fh.read()
    text = text.replace("\r\n", "\n").strip()
    if len(text) > RELEASE_NOTES_LIMIT:
        # Failing a release over the length of its notes would be out of all
        # proportion: the build is good, and the pipeline's notes are a pull
        # request description nobody wrote with Play's limit in mind.
        cut = text[:RELEASE_NOTES_LIMIT - 1].rstrip() + "…"
        gh_warning(f"Release notes are {len(text)} characters and Play accepts {RELEASE_NOTES_LIMIT}; "
                   "they were shortened.")
        text = cut
    return text


def build_release(version_codes: list[str], notes: str) -> dict:
    status = env("PLAY_STATUS", "completed")
    if status not in STATUSES:
        raise ActionError(f"status must be one of {', '.join(STATUSES)}; read '{status}'.")

    release: dict = {"versionCodes": version_codes, "status": status}

    fraction = env("PLAY_USER_FRACTION")
    if fraction:
        try:
            value = float(fraction)
        except ValueError:
            raise ActionError(f"user-fraction must be a number between 0 and 1; read '{fraction}'.")
        if not 0 < value < 1:
            raise ActionError(f"user-fraction must be greater than 0 and less than 1; read '{fraction}'. "
                              "For a full rollout leave it empty and use status: completed.")
        if status not in ("inProgress", "halted"):
            raise ActionError(f"user-fraction applies to a staged rollout only, so it needs status: inProgress "
                              f"(or halted); status is '{status}'.")
        release["userFraction"] = value
    elif status == "inProgress":
        raise ActionError("status: inProgress is a staged rollout and needs user-fraction (e.g. 0.1 for 10%).")

    name = env("PLAY_RELEASE_NAME")
    if name:
        release["name"] = name[:50]

    priority = env("PLAY_IN_APP_UPDATE_PRIORITY")
    if priority:
        if not priority.isdigit() or not 0 <= int(priority) <= 5:
            raise ActionError(f"in-app-update-priority must be a whole number from 0 to 5; read '{priority}'.")
        release["inAppUpdatePriority"] = int(priority)

    if notes:
        language = env("PLAY_RELEASE_NOTES_LANGUAGE", "en-US")
        release["releaseNotes"] = [{"language": language, "text": notes}]
    return release


def artifact_kind(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".aab"):
        return "bundle"
    if lower.endswith(".apk"):
        return "apk"
    raise ActionError(f"artifact must be an .aab or an .apk; read '{path}'.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def run() -> None:
    artifact = env("PLAY_ARTIFACT")
    if not artifact:
        raise ActionError("artifact is required: the path to the .aab or .apk to upload.")
    kind = artifact_kind(artifact)
    if not os.path.isfile(artifact):
        directory = os.path.dirname(artifact) or "."
        listing = ", ".join(sorted(os.listdir(directory))) if os.path.isdir(directory) else "directory does not exist"
        raise ActionError(f"Artifact not found at '{artifact}'. In '{directory}': {listing}.")

    track = env("PLAY_TRACK", "internal")
    if not track:
        raise ActionError("track must not be empty.")

    mapping = env("PLAY_MAPPING_FILE")
    symbols = env("PLAY_NATIVE_DEBUG_SYMBOLS")
    for label, path in (("mapping-file", mapping), ("native-debug-symbols", symbols)):
        if path and not os.path.isfile(path):
            raise ActionError(f"{label} '{path}' was not found.")

    # Everything that can be checked without Google is checked before the
    # first request, so a typo costs seconds, not an edit.
    notes = read_release_notes()
    build_release(["0"], notes)

    detail = "its manifest names no package"
    try:
        found = package_from_artifact(artifact)
    except (zipfile.BadZipFile, ValueError, struct.error, IndexError, UnicodeDecodeError) as exc:
        found = ""
        detail = str(exc)
    package = env("PLAY_PACKAGE_NAME")
    if package and found and package != found:
        raise ActionError(f"package-name is '{package}' but the artifact belongs to '{found}'. "
                          "Check that the artifact is the flavor you meant to ship.")
    if not package and not found:
        raise ActionError(f"Could not read the package name from '{artifact}' ({detail}). "
                          "Set package-name.")
    package = package or found
    print(f"Package: {package}", flush=True)

    creds = load_credentials(os.environ.get("PLAY_CREDENTIALS", "").strip(), env("PLAY_CREDENTIALS_FILE"))
    print(f"Service account: {creds['client_email']}", flush=True)
    client = PlayClient(access_token(creds), package)

    edit_id = client.call("Opening an edit", "POST", "/edits", body={})["id"]
    print(f"Edit: {edit_id}", flush=True)
    committed = False
    try:
        if kind == "bundle":
            print(f"Uploading bundle {artifact} ({os.path.getsize(artifact)} bytes)…", flush=True)
            result = client.upload("Uploading the bundle", f"/edits/{edit_id}/bundles", artifact,
                                   "application/octet-stream")
        else:
            print(f"Uploading APK {artifact} ({os.path.getsize(artifact)} bytes)…", flush=True)
            result = client.upload("Uploading the APK", f"/edits/{edit_id}/apks", artifact,
                                   "application/vnd.android.package-archive")
        version_code = str(result.get("versionCode", ""))
        if not version_code:
            raise ActionError(f"Play accepted the upload but returned no version code: {result}")
        print(f"Version code: {version_code}", flush=True)

        # Both go under "apks/" in the path, bundles included; that is how
        # the API spells it.
        if mapping:
            print(f"Uploading mapping file {mapping}…", flush=True)
            client.upload("Uploading the mapping file",
                          f"/edits/{edit_id}/apks/{version_code}/deobfuscationFiles/proguard",
                          mapping, "application/octet-stream")
        if symbols:
            print(f"Uploading native debug symbols {symbols}…", flush=True)
            client.upload("Uploading the native debug symbols",
                          f"/edits/{edit_id}/apks/{version_code}/deobfuscationFiles/nativeCode",
                          symbols, "application/octet-stream")

        release = build_release([version_code], notes)
        print(f"Assigning version code {version_code} to track '{track}' (status {release['status']})…", flush=True)
        track_path = "/edits/{}/tracks/{}".format(edit_id, urllib.parse.quote(track, safe=""))
        client.call(f"Updating track '{track}'", "PUT", track_path,
                    body={"track": track, "releases": [release]})

        query = {}
        if env("PLAY_CHANGES_NOT_SENT_FOR_REVIEW").lower() == "true":
            query["changesNotSentForReview"] = "true"
        # Not retried: a commit that reached Play and lost its answer would
        # be refused the second time with "edit not found", hiding success.
        client.call("Committing the edit", "POST", f"/edits/{edit_id}:commit", body={},
                    query=query or None, retries=0)
        committed = True
    finally:
        if not committed:
            # Best effort. An abandoned edit expires on its own, but deleting
            # it keeps the Console's "changes ready to publish" clean, and its
            # failure must not hide the error that got us here.
            try:
                client.call("Discarding the edit", "DELETE", f"/edits/{edit_id}", retries=0)
                print(f"Discarded edit {edit_id}; nothing was published.", flush=True)
            except ActionError as exc:
                print(f"Could not discard edit {edit_id}: {exc}", flush=True)

    gh_output("package-name", package)
    gh_output("version-code", version_code)
    gh_output("track", track)
    gh_output("status", release["status"])
    print(f"Published version code {version_code} of {package} to '{track}' ({release['status']}).", flush=True)
    gh_summary(f"Google Play: **{package}** version code **{version_code}** → track `{track}` "
               f"({release['status']}"
               + (f", {release['userFraction']:.0%} of users" if "userFraction" in release else "")
               + ")")


def main() -> int:
    try:
        run()
    except ActionError as exc:
        gh_error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
