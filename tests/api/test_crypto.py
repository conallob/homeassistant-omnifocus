"""Tests for the OmniFileStore crypto layer."""

from __future__ import annotations

import os

import pytest

from custom_components.omnifocus.api.crypto import (
    SEG_PAGE_SIZE,
    DocumentKey,
    EncryptionMetadata,
    Slot,
    SlotType,
    build_encryption_metadata,
    create_id,
    decrypt_file,
    encrypt_file,
    marshal_slots,
    parse_encryption_metadata,
    parse_slots,
)
from custom_components.omnifocus.api.errors import (
    OmniFocusCryptoError,
    OmniFocusPassphraseError,
)


def _key() -> DocumentKey:
    return DocumentKey([Slot(SlotType.ACTIVE_AES_CTR_HMAC, 7, os.urandom(32))])


@pytest.mark.parametrize(
    "size",
    [0, 1, 15, 16, SEG_PAGE_SIZE - 1, SEG_PAGE_SIZE, SEG_PAGE_SIZE + 1, 3 * SEG_PAGE_SIZE + 17],
)
def test_round_trip_at_segment_boundaries(size: int) -> None:
    key = _key()
    plain = os.urandom(size)
    blob = encrypt_file(key, "x.zip", plain)
    assert blob != plain
    assert decrypt_file(key, "x.zip", blob) == plain


def test_tamper_detected() -> None:
    key = _key()
    blob = bytearray(encrypt_file(key, "x.zip", b"hello world" * 100))
    blob[60] ^= 0x01
    with pytest.raises(OmniFocusCryptoError):
        decrypt_file(key, "x.zip", bytes(blob))


def test_truncated_file_rejected() -> None:
    key = _key()
    blob = encrypt_file(key, "x.zip", b"hello")
    with pytest.raises(OmniFocusCryptoError):
        decrypt_file(key, "x.zip", blob[:-5])


def test_unencrypted_file_rejected_without_mask() -> None:
    with pytest.raises(OmniFocusCryptoError):
        decrypt_file(_key(), "x.zip", b"PK\x03\x04plain")


def test_plaintext_mask_policy() -> None:
    key = DocumentKey(
        [
            Slot(SlotType.ACTIVE_AES_CTR_HMAC, 1, os.urandom(32)),
            Slot(SlotType.PLAINTEXT_MASK, 2, b".client\x00"),
        ]
    )
    assert key.plaintext_policy("a=b.client") == "plaintext"
    assert encrypt_file(key, "a=b.client", b"<plist/>") == b"<plist/>"
    assert decrypt_file(key, "a=b.client", b"<plist/>") == b"<plist/>"


def test_slots_round_trip() -> None:
    slots = [Slot(3, 1, os.urandom(32)), Slot(5, 2, b".foo\x00\x00\x00\x00")]
    assert parse_slots(marshal_slots(slots)) == slots


@pytest.mark.parametrize("prf", [None, "sha1", "sha256"])
def test_passphrase_pipeline(prf: str | None) -> None:
    from cryptography.hazmat.primitives.keywrap import aes_key_wrap

    slots = [Slot(SlotType.ACTIVE_AES_CTR_HMAC, 1, os.urandom(32))]
    meta = EncryptionMetadata("password", "PBKDF2; aes128-wrap", 5, os.urandom(16), b"", prf)
    kek = DocumentKey.derive_wrapping_key(meta, "pässword")
    meta.key = aes_key_wrap(kek, marshal_slots(slots))
    meta2 = parse_encryption_metadata(build_encryption_metadata(meta))
    assert DocumentKey.from_metadata(meta2, "pässword").slots == slots
    with pytest.raises(OmniFocusPassphraseError):
        DocumentKey.from_metadata(meta2, "wrong")


def test_ids_are_valid_names() -> None:
    ids = {create_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 11 and i[0].isalpha() for i in ids)
