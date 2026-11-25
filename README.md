# OmniFocus for Home Assistant

[![CI](https://github.com/conallob/homeassistant-omnifocus/actions/workflows/ci.yml/badge.svg)](https://github.com/conallob/homeassistant-omnifocus/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/conallob/homeassistant-omnifocus/graph/badge.svg)](https://codecov.io/gh/conallob/homeassistant-omnifocus)
[![Latest release](https://img.shields.io/github/v/release/conallob/homeassistant-omnifocus?include_prereleases)](https://github.com/conallob/homeassistant-omnifocus/releases)
[![License: BSD 3-Clause](https://img.shields.io/github/license/conallob/homeassistant-omnifocus)](LICENSE)
[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.3%2B-41BDF5.svg?logo=homeassistant&logoColor=white)](https://www.home-assistant.io)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Open issues](https://img.shields.io/github/issues/conallob/homeassistant-omnifocus)](https://github.com/conallob/homeassistant-omnifocus/issues)
[![Last commit](https://img.shields.io/github/last-commit/conallob/homeassistant-omnifocus)](https://github.com/conallob/homeassistant-omnifocus/commits/main)

Sync Home Assistant **to-do lists** with [OmniFocus](https://www.omnigroup.com/omnifocus) by
talking directly to the **Omni Sync Server** over WebDAV: no Mac, no running OmniFocus app.

Inspired by [omnifocus-sync-mcp](https://github.com/rosskukulinski/omnifocus-sync-mcp), whose
protocol work (Digest auth, OmniFileStore end-to-end encryption, transaction replay) this
integration ports to Python. Not affiliated with The Omni Group.

> ⚠️ **Early / use at your own risk.** Write support appends transactions to your real
> OmniFocus database. **Back it up first** (OmniFocus → File → Back Up Database), try a
> throwaway account, and note every list defaults to **read-only**.

## Features

- **Credentials in Application Credentials**: your sync username/password are kept in Home
  Assistant's credential store, not in the config entry.
- **Read-only or read/write, per list** (default read-only). An account whose lists are all
  read-only never writes a single byte to the server.
- **1:1 mapping**: each HA to-do list maps to exactly one OmniFocus **Inbox**, **Project** or
  **custom Perspective**. The flow refuses to map the same source twice.
- End-to-end encrypted databases supported (passphrase defaults to your sync password).
- Incremental sync: only new transactions are downloaded after the first load.

## Install

HACS → Custom repositories → add this repo as *Integration*, or copy
`custom_components/omnifocus` into your config directory. Requires a current Home Assistant
(2026.3+, i.e. Python 3.14).

## Credentials

1. **Settings → Devices & services → Application credentials → Add**, choose *OmniFocus*.
2. **Client ID** = your Omni Sync Server **username**; **Client secret** = its **password**.
   (This is not OAuth; Home Assistant is only used as the secure vault.)
3. **Add integration → OmniFocus**, pick the credential. Fill *Encryption passphrase* only if
   it differs from your sync password.

## Mapping lists

On the OmniFocus entry choose **Add to-do list**:

| Source | Items shown | Add | Edit / complete / delete |
| --- | --- | --- | --- |
| Inbox | inbox tasks | ✅ | ✅ |
| Project | leaf actions anywhere under the project, in outline order | ✅ (to the project) | ✅ |
| Perspective | tasks matching the perspective's filter rules (best-effort) | ❌ | ✅ |

Each list has a **mode**: *Read only* (default) or *Read / write*. Completed tasks stay visible
for 7 days so you can un-tick them. Due dates are OmniFocus floating (wall-clock) times in
Home Assistant's time zone; a date-only due date from HA is stored as 17:00.

## Troubleshooting

**"The Omni Sync Server rejected the login"** means the HTTP login failed, before the
encryption passphrase is even used. Two different secrets are involved:

| Where | What to enter |
| --- | --- |
| Application credential **Client ID** | your Omni Sync Server **account name** |
| Application credential **Client secret** | your sync account **password** (used to log in) |
| Integration form *Encryption passphrase* | only if it differs from the password; unlocks your data |

Check the account and password outside Home Assistant (replace `USER` and `PASS`; `207` means
the login works, `401` a wrong password, `404` an unknown account name):

```bash
curl -s --digest -u 'USER:PASS' --location-trusted -X PROPFIND -H 'Depth: 0' \
  https://sync.omnigroup.com/USER/OmniFocus.ofocus/ -o /dev/null -w '%{http_code}\n'
```

The Home Assistant log names the account that was tried (never the password). For more detail
enable `custom_components.omnifocus: debug` under `logger:`.

## Known limitations

- **Perspectives are best-effort.** OmniFocus does not document its perspective rule
  vocabulary. Rules we don't understand are listed in the entity's
  `unsupported_perspective_rules` attribute (and are *not* applied, so the list may be a
  superset). Please open an issue with the attribute value.
- **Repeating tasks can't be completed from HA** (it would need to generate the next
  occurrence); complete them in OmniFocus. Editing other fields works.
- Subtasks are flattened; tags, flags, defer dates and estimates are not exposed.
- Not tested against a live Omni Sync Server by the author of this integration; the protocol
  layer is a port of a client that was. The automated tests run the full stack (Digest auth,
  shard redirect, encryption, WebDAV, Home Assistant flows) against an in-memory fake server.

## Safety

Writes are append-only transactions chained to the current head, and this client keeps a
single `.client` registration like a normal OmniFocus device. A bad write forks history
rather than destroying data and OmniFocus merges forks on its next sync, but that is a safety
net, not a guarantee.

## Development

```bash
pip install -r requirements_test.txt   # Python 3.14
ruff check . && ruff format --check .
pytest
```

`custom_components/omnifocus/api/` is a pure-Python client with no Home Assistant imports.

## Releasing

The release tag is the single source of truth for the version; `manifest.json` in the repo
carries a `0.0.0` placeholder. To cut a release (after CI is green on `main`):

```bash
gh release create v0.0.3 --target main --generate-notes
```

The *Release* workflow runs the tests, stamps the tag's version into `manifest.json`, builds
`omnifocus.zip` and attaches it to the release. HACS installs from that zip.

## Credits

[OmniGroup/OmniGroup](https://github.com/omnigroup/OmniGroup) (encryption format),
[rosskukulinski/omnifocus-sync-mcp](https://github.com/rosskukulinski/omnifocus-sync-mcp),
[tomzx/ofocus-format](https://github.com/tomzx/ofocus-format),
[rubyfocus](https://github.com/jyruzicka/rubyfocus).

## Trademarks

OmniFocus and the OmniFocus icon are trademarks of the Omni Group. The icon in
`custom_components/omnifocus/brand/` is the Omni Group's, used under their
[press guidelines](https://www.omnigroup.com/press/) to refer to their software; it is **not**
covered by this project's license, and this project is not affiliated with or endorsed by
the Omni Group.

The code is licensed under the [BSD 3-Clause License](LICENSE).
