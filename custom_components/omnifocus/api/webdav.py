"""Minimal async WebDAV client for the Omni Sync Server.

Handles HTTP Digest authentication (what Omni Sync Server negotiates), manual
following of the shard redirect (``sync.omnigroup.com`` -> ``syncN...``) and the
handful of verbs OmniFocus sync needs.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree as ET

import aiohttp
from yarl import URL

from .errors import OmniFocusAuthError, OmniFocusConnectionError

_LOGGER = logging.getLogger(__name__)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/><D:getcontentlength/>'
    "</D:prop></D:propfind>"
)
_DAV = "{DAV:}"


@dataclass(slots=True)
class DavEntry:
    """One child of a WebDAV collection."""

    href: str
    name: str
    is_collection: bool
    content_length: int | None = None


@dataclass(slots=True)
class Challenge:
    """A parsed ``WWW-Authenticate: Digest`` challenge."""

    realm: str
    nonce: str
    qop: str | None = None
    opaque: str | None = None
    algorithm: str | None = None


def parse_digest_challenge(header: str) -> Challenge | None:
    """Parse a ``WWW-Authenticate`` header value; ``None`` if not Digest."""
    match = re.match(r"^\s*Digest\s+(.*)$", header, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    params = {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', match.group(1))
    }
    if "realm" not in params or "nonce" not in params:
        return None
    return Challenge(
        realm=params["realm"],
        nonce=params["nonce"],
        qop=params.get("qop"),
        opaque=params.get("opaque"),
        algorithm=params.get("algorithm"),
    )


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def build_digest_auth(
    challenge: Challenge, method: str, uri: str, username: str, password: str, nc: int
) -> str:
    """Build an RFC 2617 MD5 ``Authorization: Digest`` header (qop=auth)."""
    cnonce = secrets.token_hex(8)
    nc_str = f"{nc:08x}"
    ha1 = _md5(f"{username}:{challenge.realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    qop = (
        "auth"
        if challenge.qop and "auth" in [q.strip() for q in challenge.qop.split(",")]
        else None
    )
    if qop:
        response = _md5(f"{ha1}:{challenge.nonce}:{nc_str}:{cnonce}:{qop}:{ha2}")
    else:
        response = _md5(f"{ha1}:{challenge.nonce}:{ha2}")
    parts = [
        f'username="{username}"',
        f'realm="{challenge.realm}"',
        f'nonce="{challenge.nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
    ]
    if challenge.algorithm:
        parts.append(f"algorithm={challenge.algorithm}")
    if qop:
        parts += [f"qop={qop}", f"nc={nc_str}", f'cnonce="{cnonce}"']
    if challenge.opaque:
        parts.append(f'opaque="{challenge.opaque}"')
    return "Digest " + ", ".join(parts)


class WebDavClient:
    """Async WebDAV client with Digest auth and shard-redirect following."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        user_agent: str = "homeassistant-omnifocus (WebDAV)",
        max_redirects: int = 5,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._user_agent = user_agent
        self._max_redirects = max_redirects
        self._challenges: dict[str, Challenge] = {}
        self._nc: dict[str, int] = {}

    def _auth_header(self, method: str, url: URL) -> str | None:
        origin = url.origin().human_repr()
        challenge = self._challenges.get(origin)
        if challenge is None:
            return None
        self._nc[origin] = self._nc.get(origin, 0) + 1
        return build_digest_auth(
            challenge, method, url.raw_path_qs, self._username, self._password, self._nc[origin]
        )

    async def request(
        self,
        method: str,
        raw_url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | str | None = None,
    ) -> tuple[int, bytes, str]:
        """Perform a request. Returns ``(status, body, final_url)``."""
        # encoded=True keeps percent-escapes (e.g. %40) exactly as built; yarl would
        # otherwise rewrite them, and the Digest ``uri`` must match the request line.
        url = URL(raw_url, encoded=True)
        redirects = 0
        auth_attempts = 0
        while True:
            req_headers = {"User-Agent": self._user_agent, **(headers or {})}
            auth = self._auth_header(method, url)
            if auth:
                req_headers["Authorization"] = auth
            try:
                async with self._session.request(
                    method, url, headers=req_headers, data=data, allow_redirects=False
                ) as resp:
                    body = await resp.read()
                    status = resp.status
                    reason = resp.reason or ""
                    location = resp.headers.get("Location")
                    www_auth = resp.headers.get("WWW-Authenticate")
            except (aiohttp.ClientError, TimeoutError) as err:
                raise OmniFocusConnectionError(f"{method} {url.host}: {err}") from err

            if status == 404 and "no such user" in reason.lower():
                # Omni Sync Server answers this (before auth) for unknown account names.
                raise OmniFocusAuthError(f"No Omni Sync Server account named in {url.path!r}")

            if status in _REDIRECT_STATUSES:
                if not location:
                    raise OmniFocusConnectionError(f"Redirect {status} without Location")
                redirects += 1
                if redirects > self._max_redirects:
                    raise OmniFocusConnectionError("Too many redirects")
                url = URL(urljoin(str(url), location), encoded=True)
                auth_attempts = 0
                continue

            if status == 401:
                challenge = parse_digest_challenge(www_auth) if www_auth else None
                stale = bool(www_auth and "stale=true" in www_auth.lower())
                if challenge and (auth_attempts == 0 or (stale and auth_attempts < 2)):
                    origin = url.origin().human_repr()
                    self._challenges[origin] = challenge
                    self._nc[origin] = 0
                    auth_attempts += 1
                    continue
                _LOGGER.debug(
                    "401 from %s for %s (digest challenge: %s, attempts: %d, stale: %s)",
                    url.host,
                    method,
                    "yes" if challenge else "no/unsupported",
                    auth_attempts,
                    stale,
                )
                raise OmniFocusAuthError(
                    f"Sync server {url.host} rejected the credentials (HTTP 401)"
                )

            return status, body, str(url)

    @staticmethod
    def _expect(status: int, method: str, url: str, ok: tuple[int, ...]) -> None:
        if status not in ok:
            path = urlsplit(url).path
            raise OmniFocusConnectionError(f"{method} {path} failed: HTTP {status}")

    async def propfind(self, url: str) -> list[DavEntry]:
        """List the immediate children of a collection (Depth: 1)."""
        status, body, _ = await self.request(
            "PROPFIND",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data=_PROPFIND_BODY,
        )
        self._expect(status, "PROPFIND", url, (207,))
        return self._parse_multistatus(body, url)

    @staticmethod
    def _parse_multistatus(body: bytes, request_url: str) -> list[DavEntry]:
        try:
            root = ET.fromstring(body)  # noqa: S314 - response from the authenticated sync host
        except ET.ParseError as err:
            raise OmniFocusConnectionError(f"Invalid PROPFIND response: {err}") from err
        self_path = urlsplit(request_url).path.rstrip("/")
        entries: list[DavEntry] = []
        for resp in root.iter(f"{_DAV}response"):
            href_el = resp.find(f"{_DAV}href")
            if href_el is None or not href_el.text:
                continue
            href = unquote(urlsplit(href_el.text.strip()).path)
            path = href.rstrip("/")
            if path == unquote(self_path):
                continue
            prop = resp.find(f"{_DAV}propstat/{_DAV}prop")
            is_collection = (
                prop is not None and prop.find(f"{_DAV}resourcetype/{_DAV}collection") is not None
            )
            length_el = prop.find(f"{_DAV}getcontentlength") if prop is not None else None
            length = (
                int(length_el.text)
                if length_el is not None and (length_el.text or "").isdigit()
                else None
            )
            entries.append(DavEntry(href, path.rsplit("/", 1)[-1], is_collection, length))
        return entries

    async def get(self, url: str) -> bytes:
        status, body, _ = await self.request("GET", url)
        self._expect(status, "GET", url, (200,))
        return body

    async def get_optional(self, url: str) -> bytes | None:
        """GET a file, returning ``None`` if it does not exist."""
        status, body, _ = await self.request("GET", url)
        if status == 404:
            return None
        self._expect(status, "GET", url, (200,))
        return body

    async def exists(self, url: str) -> bool:
        status, _, _ = await self.request("PROPFIND", url, headers={"Depth": "0"})
        if status in (200, 207):
            return True
        if status == 404:
            return False
        raise OmniFocusConnectionError(f"PROPFIND {urlsplit(url).path} failed: HTTP {status}")

    async def put(
        self, url: str, body: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        status, _, _ = await self.request(
            "PUT", url, headers={"Content-Type": content_type}, data=body
        )
        self._expect(status, "PUT", url, (200, 201, 204))

    async def mkcol(self, url: str) -> None:
        status, _, _ = await self.request("MKCOL", url)
        self._expect(status, "MKCOL", url, (200, 201))

    async def delete(self, url: str) -> None:
        status, _, _ = await self.request("DELETE", url)
        self._expect(status, "DELETE", url, (200, 202, 204, 404))
