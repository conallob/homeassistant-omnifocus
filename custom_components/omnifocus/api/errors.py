"""Exceptions raised by the OmniFocus sync client."""

from __future__ import annotations


class OmniFocusError(Exception):
    """Base class for all OmniFocus client errors."""


class OmniFocusConnectionError(OmniFocusError):
    """The sync server could not be reached or returned an unexpected status."""


class OmniFocusAuthError(OmniFocusError):
    """The sync server rejected the username or password."""


class OmniFocusCryptoError(OmniFocusError):
    """An encrypted file was corrupt, tampered with or unreadable."""


class OmniFocusPassphraseError(OmniFocusCryptoError):
    """The encryption passphrase could not unlock the database key."""


class OmniFocusReadOnlyError(OmniFocusError):
    """A write was attempted while the client is in read-only mode."""


class OmniFocusNotFoundError(OmniFocusError):
    """A task, project or perspective no longer exists."""
