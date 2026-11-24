# OmniFocus for Home Assistant

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

## Credits

[OmniGroup/OmniGroup](https://github.com/omnigroup/OmniGroup) (encryption format),
[rosskukulinski/omnifocus-sync-mcp](https://github.com/rosskukulinski/omnifocus-sync-mcp),
[tomzx/ofocus-format](https://github.com/tomzx/ofocus-format),
[rubyfocus](https://github.com/jyruzicka/rubyfocus).

MIT licensed.
