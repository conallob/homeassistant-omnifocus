"""An OmniFocus sync store (``OmniFocus.ofocus``) on a WebDAV server."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .crypto import (
    DocumentKey,
    create_id,
    decrypt_file,
    encrypt_file,
    parse_encryption_metadata,
)
from .errors import OmniFocusCryptoError, OmniFocusError
from .webdav import WebDavClient

ENCRYPTED_METADATA = "encrypted"
_TXN_RE = re.compile(r"^(\d{14})=(.+)\.zip$")
_MAX_UNZIPPED = 256 * 1024 * 1024  # guard against zip bombs


@dataclass(slots=True)
class TransactionFile:
    """A parsed transaction file name: ``<utc stamp>=<parent>+...+<tail>.zip``."""

    name: str
    timestamp: str
    parents: list[str]
    tail: str

    @property
    def is_baseline(self) -> bool:
        return self.timestamp == "00000000000000"


@dataclass(slots=True)
class StoreListing:
    """A snapshot of the store's directory."""

    transactions: list[TransactionFile] = field(default_factory=list)
    client_files: list[str] = field(default_factory=list)
    encrypted: bool = False


def parse_txn_name(name: str) -> TransactionFile | None:
    """Parse a transaction file name; ``None`` if it is not one."""
    match = _TXN_RE.match(name)
    if not match:
        return None
    ids = match.group(2).split("+")
    return TransactionFile(name, match.group(1), ids[:-1], ids[-1])


def utc_stamp(when: datetime | None = None) -> str:
    """14-digit UTC stamp used in file names."""
    return (when or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d%H%M%S")


def order_transactions(transactions: list[TransactionFile]) -> list[TransactionFile]:
    """Order by timestamp, and by parent chain within the same second.

    File names only carry one-second resolution, so two transactions written in
    the same second would otherwise sort by their random ids and could replay
    out of order.
    """
    ordered: list[TransactionFile] = []
    by_stamp: dict[str, list[TransactionFile]] = {}
    for txn in sorted(transactions, key=lambda t: t.name):
        by_stamp.setdefault(txn.timestamp, []).append(txn)
    for stamp in sorted(by_stamp):
        group = by_stamp[stamp]
        tails = {t.tail for t in group}
        done: set[str] = set()
        pending = list(group)
        while pending:
            # Emit one whose in-group parents are all already emitted.
            ready = next(
                (t for t in pending if all(p in done or p not in tails for p in t.parents)),
                pending[0],  # cycle/garbage: fall back to name order
            )
            pending.remove(ready)
            done.add(ready.tail)
            ordered.append(ready)
    return ordered


def compute_head_tail(transactions: list[TransactionFile]) -> str | None:
    """The tail id that no other transaction lists as a parent (newest wins)."""
    if not transactions:
        return None
    parents = {p for t in transactions for p in t.parents}
    heads = [t for t in transactions if t.tail not in parents] or transactions
    return max(heads, key=lambda t: t.name).tail


class OmniFocusStore:
    """List, read and write transactions in a (possibly encrypted) store."""

    def __init__(self, dav: WebDavClient, ofocus_url: str) -> None:
        self._dav = dav
        self._url = ofocus_url.rstrip("/")
        self._doc_key: DocumentKey | None = None

    def _child(self, name: str) -> str:
        return f"{self._url}/{name}"

    @property
    def is_encrypted(self) -> bool:
        return self._doc_key is not None

    async def unlock(self, passphrase: str, run_in_executor) -> None:
        """Fetch ``encrypted`` metadata and unwrap the keys with the passphrase.

        PBKDF2 is CPU heavy, so the key derivation runs via ``run_in_executor``.
        """
        raw = await self._dav.get_optional(self._child(ENCRYPTED_METADATA))
        if raw is None:
            self._doc_key = None
            return
        meta = parse_encryption_metadata(raw)
        self._doc_key = await run_in_executor(DocumentKey.from_metadata, meta, passphrase)

    async def list(self) -> StoreListing:
        """List transactions (chronological) and client files."""
        listing = StoreListing()
        for entry in await self._dav.propfind(self._url + "/"):
            if entry.is_collection:
                continue
            if entry.name == ENCRYPTED_METADATA:
                listing.encrypted = True
            elif entry.name.endswith(".client"):
                listing.client_files.append(entry.name)
            elif (txn := parse_txn_name(entry.name)) is not None:
                listing.transactions.append(txn)
        listing.transactions = order_transactions(listing.transactions)
        return listing

    def _decode(self, name: str, data: bytes) -> bytes:
        return decrypt_file(self._doc_key, name, data) if self._doc_key else data

    async def read_contents_raw(self, name: str) -> bytes:
        """Download a transaction, returning the encrypted-or-plain zip bytes."""
        return await self._dav.get(self._child(name))

    def contents_from_raw(self, name: str, raw: bytes) -> str:
        """Decrypt (if needed) and unzip a downloaded transaction (CPU bound)."""
        data = self._decode(name, raw)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                info = archive.getinfo("contents.xml")
                if info.file_size > _MAX_UNZIPPED:
                    raise OmniFocusCryptoError(f"{name}: contents.xml is implausibly large")
                return archive.read(info).decode("utf-8")
        except (zipfile.BadZipFile, KeyError) as err:
            raise OmniFocusError(f"{name}: not a valid transaction archive ({err})") from err

    async def write_transaction(self, contents_xml: str, parents: list[str]) -> tuple[str, str]:
        """Upload a new transaction descending from ``parents``; returns ``(name, tail)``."""
        tail = create_id()
        name = f"{utc_stamp()}={'+'.join([*parents, tail])}.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("contents.xml", contents_xml.encode("utf-8"))
        payload = buf.getvalue()
        if self._doc_key:
            payload = encrypt_file(self._doc_key, name, payload)
        await self._dav.put(self._child(name), payload)
        return name, tail

    async def write_client_file(self, client_id: str, plist_xml: bytes) -> str:
        """Write a fresh ``.client`` registration file and prune our older ones."""
        name = f"{utc_stamp()}={client_id}.client"
        payload = plist_xml
        if self._doc_key:
            payload = encrypt_file(self._doc_key, name, plist_xml)
        await self._dav.put(self._child(name), payload)
        return name

    async def delete(self, name: str) -> None:
        await self._dav.delete(self._child(name))
