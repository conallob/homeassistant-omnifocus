"""API tests spin up a local fake sync server."""

import pytest


@pytest.fixture(autouse=True)
def _allow_local_sockets(socket_enabled):
    """Loopback sockets are needed for the fake server."""
