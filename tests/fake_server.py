"""An in-memory fake Omni Sync Server for tests.

Speaks just enough WebDAV (PROPFIND/GET/PUT/DELETE/MKCOL) with HTTP Digest auth
and a shard redirect, and serves a real OmniFileStore-encrypted database.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from xml.sax.saxutils import escape

from aiohttp import web
from cryptography.hazmat.primitives.keywrap import aes_key_wrap

from custom_components.omnifocus.api.crypto import (
    DocumentKey,
    EncryptionMetadata,
    Slot,
    SlotType,
    build_encryption_metadata,
    encrypt_file,
    marshal_slots,
)

USER = "alice"
PASSWORD = "s3cret-pw"
REALM = "Omni Sync"
NONCE = "abc123nonce"
NS = "http://www.omnigroup.com/namespace/OmniFocus/v2"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode(), usedforsecurity=False).hexdigest()


def contents(*elements: str) -> str:
    """Wrap elements in an <omnifocus> document."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><omnifocus xmlns="{NS}">'
        + "".join(elements)
        + "</omnifocus>"
    )


def task_xml(
    tid: str,
    name: str,
    *,
    parent: str | None = None,
    inbox: bool = False,
    project: str | None = None,
    folder: str | None = None,
    completed: str | None = None,
    due: str | None = None,
    note: str | None = None,
    rank: int = 1,
    order: str = "parallel",
    flagged: bool = False,
    repeat: bool = False,
) -> str:
    parts = [f'<task id="{tid}">']
    if project:
        f = f'<folder idref="{folder}"/>' if folder else ""
        parts.append(f"<project><status>{project}</status>{f}</project>")
    else:
        parts.append("<project/>")
    parts.append(f'<task idref="{parent}"/>' if parent else "<task/>")
    if inbox:
        parts.append("<inbox>true</inbox>")
    parts.append(f"<name>{escape(name)}</name>")
    if note:
        parts.append(f"<note><text><p><run><lit>{escape(note)}</lit></run></p></text></note>")
    if completed:
        parts.append(f"<completed>{completed}</completed>")
    if due:
        parts.append(f"<due>{due}</due>")
    if flagged:
        parts.append("<flagged>true</flagged>")
    if repeat:
        parts.append("<repetition-rule>FREQ=DAILY</repetition-rule>")
    parts.append(f"<rank>{rank}</rank><order>{order}</order></task>")
    return "".join(parts)


class FakeOmniServer:
    """Holds the store files and serves them over WebDAV."""

    def __init__(
        self,
        *,
        encrypted: bool = True,
        passphrase: str = PASSWORD,
        user: str = USER,
        prf: str = "sha1",
    ) -> None:
        self.files: dict[str, bytes] = {}
        self.doc_key: DocumentKey | None = None
        self.requests: list[tuple[str, str]] = []
        self.shard_hits = 0
        self.fail_auth = False
        self.user = user
        self.challenges = [f'Digest realm="{REALM}", nonce="{NONCE}", algorithm=MD5, qop="auth"']
        self.base = f"/{user}/OmniFocus.ofocus"
        if encrypted:
            self._setup_encryption(passphrase, prf)
        self._nonce_count = 0

    # ---- store content helpers -------------------------------------------------

    def _setup_encryption(self, passphrase: str, prf: str) -> None:
        salt, rounds = os.urandom(16), 10
        slots = [Slot(SlotType.ACTIVE_AES_CTR_HMAC, 1, os.urandom(32))]
        meta = EncryptionMetadata("password", "PBKDF2; aes128-wrap", rounds, salt, b"", prf)
        # Derived independently of the code under test, as Omni's DecryptionExample.py
        # does: always a 16-byte (AES-128) wrapping key, whatever the PRF.
        kek = hashlib.pbkdf2_hmac(prf, passphrase.encode("utf-8"), salt, rounds, 16)
        meta.key = aes_key_wrap(kek, marshal_slots(slots))
        self.doc_key = DocumentKey(slots)
        self.files["encrypted"] = build_encryption_metadata(meta)

    def add_transaction(self, name: str, xml: str) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("contents.xml", xml)
        data = buf.getvalue()
        if self.doc_key:
            data = encrypt_file(self.doc_key, name, data)
        self.files[name] = data

    def read_transaction(self, name: str) -> str:
        from custom_components.omnifocus.api.crypto import decrypt_file

        data = self.files[name]
        if self.doc_key:
            data = decrypt_file(self.doc_key, name, data)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.read("contents.xml").decode()

    def txn_names(self) -> list[str]:
        return sorted(n for n in self.files if n.endswith(".zip"))

    def client_names(self) -> list[str]:
        return sorted(n for n in self.files if n.endswith(".client"))

    # ---- HTTP ------------------------------------------------------------------

    def _digest_ok(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return False
        p = {m[0]: m[1] or m[2] for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header[7:])}
        if p.get("username") != self.user or p.get("nonce") != NONCE:
            return False
        if p.get("uri") != request.raw_path:
            return False
        ha1 = _md5(f"{self.user}:{REALM}:{PASSWORD}")
        ha2 = _md5(f"{request.method}:{p['uri']}")
        expect = _md5(f"{ha1}:{NONCE}:{p['nc']}:{p['cnonce']}:{p['qop']}:{ha2}")
        return p.get("response") == expect

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.method, request.path))
        # Shard redirect: entry host -> shard path prefix.
        if request.path.startswith("/entry/"):
            self.shard_hits += 1
            raise web.HTTPFound(request.path.replace("/entry/", "/", 1))
        if not request.path.startswith(f"/{self.user}/"):
            return web.Response(status=404, reason="No such user")
        if self.fail_auth or not self._digest_ok(request):
            resp = web.Response(status=401)
            for challenge in self.challenges:
                resp.headers.add("WWW-Authenticate", challenge)
            return resp
        path = request.path
        if request.method == "PROPFIND":
            return self._propfind(path, request.headers.get("Depth", "1"))
        if not path.startswith(self.base):
            return web.Response(status=404)
        name = path[len(self.base) :].lstrip("/")
        if request.method == "GET":
            if name in self.files:
                return web.Response(body=self.files[name])
            return web.Response(status=404)
        if request.method == "PUT":
            self.files[name] = await request.read()
            return web.Response(status=201)
        if request.method == "DELETE":
            self.files.pop(name, None)
            return web.Response(status=204)
        if request.method == "MKCOL":
            return web.Response(status=201)
        return web.Response(status=405)

    def _propfind(self, path: str, depth: str) -> web.Response:
        if path.rstrip("/") != self.base:
            return web.Response(status=404)
        rows = [
            f"<D:response><D:href>{self.base}/</D:href><D:propstat><D:prop>"
            "<D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>"
        ]
        if depth != "0":
            for name, data in self.files.items():
                rows.append(
                    f"<D:response><D:href>{self.base}/{name}</D:href><D:propstat><D:prop>"
                    f"<D:resourcetype/><D:getcontentlength>{len(data)}</D:getcontentlength>"
                    "</D:prop></D:propstat></D:response>"
                )
        body = (
            '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
            + "".join(rows)
            + "</D:multistatus>"
        )
        return web.Response(status=207, body=body, content_type="application/xml")

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app
