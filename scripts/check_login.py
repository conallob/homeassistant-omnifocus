#!/usr/bin/env python3
"""Check an Omni Sync Server login with this integration's own client.

Runs the same code path Home Assistant uses (Digest login, redirect to your shard,
unlock with the encryption passphrase, read the database) outside Home Assistant,
and reports which stage fails. The password is read privately and never printed.

    pip install aiohttp yarl cryptography
    python3 scripts/check_login.py YOUR_SYNC_USERNAME

Set OF_PASSWORD / OF_PASSPHRASE to skip the prompts. Leave the passphrase empty if it
is the same as the password.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "omnifocus" / "api"


def load_api():
    """Import the api package without importing Home Assistant."""
    spec = importlib.util.spec_from_file_location(
        "omnifocus_api", API_DIR / "__init__.py", submodule_search_locations=[str(API_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["omnifocus_api"] = module
    spec.loader.exec_module(module)
    return module


def describe_secret(name: str, value: str) -> list[str]:
    """Non-revealing hints about a secret that commonly explain a rejected login."""
    notes = []
    if value != value.strip():
        notes.append(f"{name} has leading/trailing whitespace")
    if not value.isascii():
        notes.append(f"{name} contains non-ASCII characters")
    if any(c in value for c in "\r\n\t"):
        notes.append(f"{name} contains a newline/tab")
    return notes


async def run(username: str, password: str, passphrase: str, server: str) -> int:
    import aiohttp

    api = load_api()
    for note in describe_secret("password", password) + describe_secret("passphrase", passphrase):
        print(f"note: {note}")
    async with aiohttp.ClientSession(trust_env=True) as session:
        client = api.OmniFocusClient(
            session,
            username=username,
            password=password,
            passphrase=passphrase or None,
            run_in_executor=lambda f, *a: asyncio.get_running_loop().run_in_executor(None, f, *a),
            client_id="checkLogin01",
            host_id="CHECK-LOGIN",
            client_name="check_login",
            registration_date=datetime.now(UTC),
            base_url=server,
            read_only=True,
        )
        try:
            await client.async_validate()
            print("OK  login accepted and encryption passphrase unlocked the database")
            snap = await client.async_refresh(
                [api.Target("inbox")], datetime.now().astimezone().tzinfo
            )
        except api.OmniFocusPassphraseError as err:
            print(f"FAIL login accepted, but the encryption passphrase is wrong: {err}")
            return 2
        except api.OmniFocusAuthError as err:
            print(f"FAIL login rejected: {err}")
            return 1
        except api.OmniFocusError as err:
            print(f"FAIL {type(err).__name__}: {err}")
            return 3
        print(
            f"OK  database read: {len(snap.projects)} projects, "
            f"{len(snap.perspectives)} perspectives, "
            f"{len(snap.results[api.Target('inbox')].items)} inbox items"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("username")
    parser.add_argument("--server", default="https://sync.omnigroup.com")
    args = parser.parse_args(argv)
    password = os.environ.get("OF_PASSWORD") or getpass.getpass("Sync account password: ")
    if "OF_PASSPHRASE" in os.environ:
        passphrase = os.environ["OF_PASSPHRASE"]
    else:
        passphrase = getpass.getpass("Encryption passphrase (empty = same as password): ")
    return asyncio.run(run(args.username, password, passphrase, args.server))


if __name__ == "__main__":
    raise SystemExit(main())
