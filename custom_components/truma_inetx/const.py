"""Constants for the Truma iNet X (BLE) integration."""

from __future__ import annotations

import logging

DOMAIN = "truma_inetx"
LOGGER = logging.getLogger(__package__)

# Config entry data keys
CONF_PASSKEY = "passkey"
# Optional: pin the BLE connection to a specific adapter via raw bleak, bypassing
# HA's shared scan+connect. Needed for weak dongles (CSR8510 clones) that can't
# scan and connect on the same radio concurrently.
CONF_ADAPTER = "adapter"

# Advertised local-name prefix used for discovery / manual matching.
LOCAL_NAME_PREFIX = "Truma iNetX"

# Manufacturer shown in the HA device registry.
MANUFACTURER = "Truma"
MODEL = "iNet X (Combi)"
