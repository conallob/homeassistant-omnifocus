"""Constants for the OmniFocus integration."""

from __future__ import annotations

DOMAIN = "omnifocus"

# Config entry data.
CONF_SERVER_URL = "server_url"
CONF_DATABASE = "database"
CONF_PASSPHRASE = "passphrase"
CONF_CLIENT_ID = "of_client_id"
CONF_HOST_ID = "of_host_id"
CONF_REGISTERED = "of_registered"
CONF_IMPLEMENTATION = "implementation"

# Config entry options.
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 60
MAX_SCAN_INTERVAL = 3600

# Subentry ("to-do list mapping") data.
SUBENTRY_TYPE_LIST = "todo_list"
CONF_NAME = "name"
CONF_TARGET_TYPE = "target_type"
CONF_PROJECT = "project"
CONF_PERSPECTIVE = "perspective"
CONF_MODE = "mode"

TARGET_INBOX = "inbox"
TARGET_PROJECT = "project"
TARGET_PERSPECTIVE = "perspective"
TARGET_TYPES = [TARGET_INBOX, TARGET_PROJECT, TARGET_PERSPECTIVE]

MODE_READ_ONLY = "read_only"
MODE_READ_WRITE = "read_write"
MODES = [MODE_READ_ONLY, MODE_READ_WRITE]
DEFAULT_MODE = MODE_READ_ONLY

# Time of day applied when Home Assistant supplies a date-only due date.
DEFAULT_DUE_HOUR = 17
