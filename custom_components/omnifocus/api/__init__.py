"""Pure-Python client for the OmniFocus sync protocol (no Home Assistant imports)."""

from .client import (
    DEFAULT_DATABASE,
    DEFAULT_SERVER,
    ItemSnapshot,
    OmniFocusClient,
    Snapshot,
    Target,
    TargetResult,
)
from .errors import (
    OmniFocusAuthError,
    OmniFocusConnectionError,
    OmniFocusCryptoError,
    OmniFocusError,
    OmniFocusNotFoundError,
    OmniFocusPassphraseError,
    OmniFocusReadOnlyError,
)

__all__ = [
    "DEFAULT_DATABASE",
    "DEFAULT_SERVER",
    "ItemSnapshot",
    "OmniFocusAuthError",
    "OmniFocusClient",
    "OmniFocusConnectionError",
    "OmniFocusCryptoError",
    "OmniFocusError",
    "OmniFocusNotFoundError",
    "OmniFocusPassphraseError",
    "OmniFocusReadOnlyError",
    "Snapshot",
    "Target",
    "TargetResult",
]
