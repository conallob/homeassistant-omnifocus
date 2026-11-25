"""OmniFileStore end-to-end encryption used by the Omni Sync Server.

Ported from the TypeScript implementation in rosskukulinski/omnifocus-sync-mcp,
itself derived from Omni's open-source ``OmniFileStore/EncryptionFormat.md``.

Layout of an encrypted file::

    magic | uint16 keyinfo-len | keyinfo (uint16 slot id) | zero pad to 16
    { iv(12) | mac(20) | AES-128-CTR ciphertext(<=64KiB) } * n
    file-mac(32)

Every segment MAC and the file MAC (HMAC-SHA256, encrypt-then-MAC) are verified
before any plaintext is released.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import plistlib
import secrets
import struct
from dataclasses import dataclass
from enum import IntEnum

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import (
    InvalidUnwrap,
    aes_key_unwrap,
    aes_key_wrap,
)

from .errors import OmniFocusCryptoError, OmniFocusPassphraseError

FILE_MAGIC = b"OmniFileEncryption\x00\x00"
LEGACY_MAGIC = b"OmniFileStore encryption\x00STRAWMAN-6"
SEG_IV_LEN = 12
SEG_MAC_LEN = 20
SEG_PAGE_SIZE = 65536
FILE_MAC_PREFIX = b"\x01"
FILE_MAC_LEN = 32


class SlotType(IntEnum):
    """Key slot types stored in the unwrapped document key blob."""

    NONE = 0
    ACTIVE_AESWRAP = 1
    RETIRED_AESWRAP = 2
    ACTIVE_AES_CTR_HMAC = 3
    RETIRED_AES_CTR_HMAC = 4
    PLAINTEXT_MASK = 5
    RETIRED_PLAINTEXT_MASK = 6


@dataclass(slots=True)
class Slot:
    """A key (or filename policy) slot."""

    type: int
    id: int
    contents: bytes


def parse_slots(unwrapped: bytes) -> list[Slot]:
    """Parse the packed slot list: ``[type u8][len u8 (x4 bytes)][id u16][data]``."""
    slots: list[Slot] = []
    idx = 0
    while idx < len(unwrapped):
        slot_type = unwrapped[idx]
        if slot_type == SlotType.NONE:
            break
        length = 4 * unwrapped[idx + 1]
        (slot_id,) = struct.unpack_from(">H", unwrapped, idx + 2)
        slots.append(Slot(slot_type, slot_id, bytes(unwrapped[idx + 4 : idx + 4 + length])))
        idx += 4 + length
    return slots


def marshal_slots(slots: list[Slot]) -> bytes:
    """Inverse of :func:`parse_slots`, padded to a multiple of 8 for key wrap."""
    out = bytearray()
    for slot in slots:
        if len(slot.contents) % 4:
            raise OmniFocusCryptoError("Slot contents length must be a multiple of 4")
        out += struct.pack(">BBH", slot.type, len(slot.contents) // 4, slot.id)
        out += slot.contents
    if len(out) % 8:
        out += bytes(8 - len(out) % 8)
    return bytes(out)


@dataclass(slots=True)
class EncryptionMetadata:
    """Contents of the ``encrypted`` plist at the root of a store."""

    method: str
    algorithm: str
    rounds: int
    salt: bytes
    key: bytes
    prf: str | None = None


def parse_encryption_metadata(data: bytes) -> EncryptionMetadata:
    """Parse the ``encrypted`` file (a 1-element array or a bare dict)."""
    try:
        parsed = plistlib.loads(data)
    except Exception as err:
        raise OmniFocusCryptoError(f"Unreadable encryption metadata: {err}") from err
    if isinstance(parsed, list):
        if len(parsed) != 1:
            raise OmniFocusCryptoError("Unexpected encryption metadata array length")
        parsed = parsed[0]
    try:
        return EncryptionMetadata(
            method=str(parsed["method"]),
            algorithm=str(parsed["algorithm"]),
            rounds=int(parsed["rounds"]),
            salt=bytes(parsed["salt"]),
            key=bytes(parsed["key"]),
            prf=str(parsed["prf"]) if parsed.get("prf") else None,
        )
    except (KeyError, TypeError, ValueError) as err:
        raise OmniFocusCryptoError(f"Malformed encryption metadata: {err}") from err


def build_encryption_metadata(meta: EncryptionMetadata) -> bytes:
    """Serialise metadata as OmniFileStore does: an array of one dictionary."""
    entry: dict[str, object] = {
        "method": meta.method,
        "algorithm": meta.algorithm,
        "rounds": meta.rounds,
        "salt": meta.salt,
        "key": meta.key,
    }
    if meta.prf:
        entry["prf"] = meta.prf
    return plistlib.dumps([entry])


class DocumentKey:
    """The unwrapped key slots for a database."""

    def __init__(self, slots: list[Slot]) -> None:
        self.slots = slots

    @staticmethod
    def derive_wrapping_key(
        meta: EncryptionMetadata, passphrase: str, *, normalize: bool = True
    ) -> bytes:
        """PBKDF2 the passphrase into the AES key-wrapping key.

        Omni's reference implementation (DecryptionExample.py) always derives
        **16 bytes** (AES-128 wrap) whatever the PRF; a longer key for the SHA-2
        PRFs would never unwrap a real database.
        """
        import unicodedata

        if meta.method != "password":
            raise OmniFocusCryptoError(f"Unsupported key method: {meta.method}")
        if meta.algorithm != "PBKDF2; aes128-wrap":
            raise OmniFocusCryptoError(f"Unsupported key algorithm: {meta.algorithm}")
        prf = meta.prf or "sha1"
        if prf not in ("sha1", "sha256", "sha512"):
            raise OmniFocusCryptoError(f"Unsupported PRF: {prf}")
        text = unicodedata.normalize("NFC", passphrase) if normalize else passphrase
        return hashlib.pbkdf2_hmac(prf, text.encode("utf-8"), meta.salt, meta.rounds, 16)

    @classmethod
    def from_metadata(cls, meta: EncryptionMetadata, passphrase: str) -> DocumentKey:
        """Unwrap the document keys; a wrong passphrase fails the RFC 3394 check.

        Omni's own code is ambiguous about Unicode normalisation (the spec says NFC
        is ideal, the example does not, and macOS text input is often decomposed), so
        a passphrase whose raw, NFC and NFD forms differ is tried each way. For plain
        ASCII that is a single attempt. The unwrap integrity check makes a false match
        negligible.
        """
        import unicodedata

        candidates = list(
            dict.fromkeys(
                [
                    passphrase,
                    unicodedata.normalize("NFC", passphrase),
                    unicodedata.normalize("NFD", passphrase),
                ]
            )
        )
        for candidate in candidates:
            kek = cls.derive_wrapping_key(meta, candidate, normalize=False)
            try:
                unwrapped = aes_key_unwrap(kek, meta.key)
            except InvalidUnwrap, InvalidKey, ValueError:
                continue
            return cls(parse_slots(unwrapped))
        raise OmniFocusPassphraseError(
            "Could not unwrap the document key: the encryption passphrase is incorrect"
        )

    def wrap(self, kek: bytes) -> bytes:
        """Re-wrap the slots for storage in the ``encrypted`` metadata."""
        return aes_key_wrap(kek, marshal_slots(self.slots))

    def slot_by_id(self, slot_id: int) -> Slot | None:
        return next((s for s in self.slots if s.id == slot_id), None)

    def active_ctr_hmac_slot(self) -> Slot | None:
        return next((s for s in self.slots if s.type == SlotType.ACTIVE_AES_CTR_HMAC), None)

    def plaintext_policy(self, filename: str) -> str | None:
        """Return ``"plaintext"``, ``"legacy"`` or ``None`` for a file name."""
        name = filename.encode("utf-8")
        policy: str | None = None
        for slot in self.slots:
            if slot.type not in (SlotType.PLAINTEXT_MASK, SlotType.RETIRED_PLAINTEXT_MASK):
                continue
            suffix = slot.contents.rstrip(b"\x00")
            if suffix and name.endswith(suffix):
                if slot.type == SlotType.PLAINTEXT_MASK:
                    policy = "plaintext"
                elif policy != "plaintext":
                    policy = "legacy"
        return policy


def _is_encrypted(data: bytes) -> bool:
    return data.startswith(FILE_MAGIC) or data.startswith(LEGACY_MAGIC)


def _file_key_material(doc_key: DocumentKey, info: bytes) -> bytes:
    if len(info) < 2:
        raise OmniFocusCryptoError("Missing per-file key info")
    (key_id,) = struct.unpack_from(">H", info, 0)
    slot = doc_key.slot_by_id(key_id)
    if slot is None:
        raise OmniFocusCryptoError(f"No key slot with id {key_id}")
    if slot.type in (SlotType.ACTIVE_AES_CTR_HMAC, SlotType.RETIRED_AES_CTR_HMAC):
        if len(info) != 2:
            raise OmniFocusCryptoError("Unexpected per-file key info for CTR+HMAC slot")
        return slot.contents
    if slot.type in (SlotType.ACTIVE_AESWRAP, SlotType.RETIRED_AESWRAP):
        wrapped = info[2:]
        if len(wrapped) % 8:
            raise OmniFocusCryptoError("Malformed AESWRAP file key")
        try:
            return aes_key_unwrap(slot.contents, wrapped)
        except (InvalidUnwrap, ValueError) as err:
            raise OmniFocusCryptoError("Could not unwrap per-file key") from err
    raise OmniFocusCryptoError(f"Unusable key slot type for decryption: {slot.type}")


def _segment_ranges(seg0_start: int, seg_n_end: int) -> list[tuple[int, int, int]]:
    """Return ``(index, start, data_len)`` for each segment."""
    hdr = SEG_IV_LEN + SEG_MAC_LEN
    full = hdr + SEG_PAGE_SIZE
    region = seg_n_end - seg0_start
    if region < 0:
        raise OmniFocusCryptoError("Encrypted file is shorter than its header + MAC")
    if region == 0:
        return []
    count = -(-region // full)
    ranges: list[tuple[int, int, int]] = []
    pos = seg0_start
    for i in range(count):
        data_len = seg_n_end - pos - hdr if i == count - 1 else SEG_PAGE_SIZE
        if data_len < 0:
            raise OmniFocusCryptoError("Truncated encrypted segment")
        ranges.append((i, pos, data_len))
        pos += hdr + data_len
    return ranges


def _ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv + bytes(4)))
    ctx = cipher.encryptor()  # CTR is symmetric
    return ctx.update(data) + ctx.finalize()


def decrypt_file(doc_key: DocumentKey, filename: str, data: bytes) -> bytes:
    """Verify and decrypt one store file, honouring plaintext filename masks."""
    if not _is_encrypted(data):
        if doc_key.plaintext_policy(filename) and b"crypt" not in data[:32]:
            return bytes(data)
        raise OmniFocusCryptoError(f"File {filename} is not encrypted (or has bad magic)")

    magic_len = len(FILE_MAGIC) if data.startswith(FILE_MAGIC) else len(LEGACY_MAGIC)
    off = magic_len
    (info_len,) = struct.unpack_from(">H", data, off)
    off += 2
    info = data[off : off + info_len]
    off += info_len
    pad_len = -off % 16
    if any(data[off : off + pad_len]):
        raise OmniFocusCryptoError("Non-zero header padding")
    off += pad_len

    material = _file_key_material(doc_key, info)
    aes_key, mac_key = material[:16], material[16:32]

    seg_end = len(data) - FILE_MAC_LEN
    if seg_end < off:
        raise OmniFocusCryptoError("Encrypted file is shorter than its header + MAC")
    ranges = _segment_ranges(off, seg_end)

    file_hash = hmac.new(mac_key, FILE_MAC_PREFIX, hashlib.sha256)
    segments: list[tuple[bytes, bytes]] = []
    for index, start, data_len in ranges:
        iv = data[start : start + SEG_IV_LEN]
        stored = data[start + SEG_IV_LEN : start + SEG_IV_LEN + SEG_MAC_LEN]
        ct_start = start + SEG_IV_LEN + SEG_MAC_LEN
        ciphertext = data[ct_start : ct_start + data_len]
        seg_hash = hmac.new(mac_key, iv + struct.pack(">I", index) + ciphertext, hashlib.sha256)
        if not hmac.compare_digest(seg_hash.digest()[:SEG_MAC_LEN], stored):
            raise OmniFocusCryptoError(f"Segment {index} MAC mismatch (corrupt or wrong key)")
        file_hash.update(stored)
        segments.append((iv, ciphertext))
    if not hmac.compare_digest(file_hash.digest(), data[seg_end:]):
        raise OmniFocusCryptoError("File MAC mismatch (corrupt or wrong key)")

    return b"".join(_ctr(aes_key, iv, ct) for iv, ct in segments)


def encrypt_file(doc_key: DocumentKey, filename: str, plaintext: bytes) -> bytes:
    """Encrypt a store file with the active CTR+HMAC key."""
    if doc_key.plaintext_policy(filename) == "plaintext":
        return bytes(plaintext)
    slot = doc_key.active_ctr_hmac_slot()
    if slot is None:
        raise OmniFocusCryptoError("No active AES_CTR_HMAC key slot for encryption")
    aes_key, mac_key = slot.contents[:16], slot.contents[16:32]

    header = FILE_MAGIC + struct.pack(">HH", 2, slot.id)
    header += bytes(-len(header) % 16)

    chunks = [header]
    file_hash = hmac.new(mac_key, FILE_MAC_PREFIX, hashlib.sha256)
    index = 0
    offset = 0
    # Always emit at least one segment, as the reference implementation does.
    while offset < len(plaintext) or index == 0:
        iv = secrets.token_bytes(SEG_IV_LEN)
        ciphertext = _ctr(aes_key, iv, plaintext[offset : offset + SEG_PAGE_SIZE])
        seg_hash = hmac.new(mac_key, iv + struct.pack(">I", index) + ciphertext, hashlib.sha256)
        seg_mac = seg_hash.digest()[:SEG_MAC_LEN]
        chunks += [iv, seg_mac, ciphertext]
        file_hash.update(seg_mac)
        offset += SEG_PAGE_SIZE
        index += 1
    chunks.append(file_hash.digest())
    return b"".join(chunks)


# Identifier alphabet: URL-safe base64 with the case swapped (OFXMLIdentifier.m).
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
_ID_LENGTH = 11


def create_id() -> str:
    """Generate an 11-character OmniFocus object id (64 random bits).

    The first character carries only 4 bits so it is always a letter, as the XML
    NAME production requires.
    """
    value = int.from_bytes(os.urandom(8), "little")
    chars = [_ID_ALPHABET[value & 0xF]]
    value >>= 4
    for _ in range(1, _ID_LENGTH):
        chars.append(_ID_ALPHABET[value & 0x3F])
        value >>= 6
    return "".join(chars)
