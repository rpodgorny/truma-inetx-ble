"""Config flow for the Truma iNet X (BLE) integration.

Tier 1 assumes the panel has already been BLE-bonded out of band (the panel
shows a 6-digit passkey; bonding is a one-time BlueZ operation). The flow here
only records which device to talk to; it does not perform pairing.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_NAME

from .const import DOMAIN, LOCAL_NAME_PREFIX


class TrumaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Truma iNet X."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        # keyed by stable device name -> latest advertisement seen
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Truma panel found by the Bluetooth integration.

        The panel uses a rotating (resolvable private) BLE address, so we key
        the unique_id on the stable advertised name and treat the address as a
        mutable connection detail. This collapses the per-address discovery
        flows into one and keeps the stored address fresh on rediscovery.
        """
        await self.async_set_unique_id(discovery_info.name)
        self._abort_if_unique_id_configured(
            updates={CONF_ADDRESS: discovery_info.address}
        )
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered panel."""
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.name,
                data={
                    CONF_ADDRESS: self._discovery_info.address,
                    CONF_NAME: self._discovery_info.name,
                },
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._discovery_info.name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup by picking from discovered Truma panels."""
        if user_input is not None:
            name = user_input[CONF_ADDRESS]
            info = self._discovered[name]
            await self.async_set_unique_id(name, raise_on_progress=False)
            self._abort_if_unique_id_configured(updates={CONF_ADDRESS: info.address})
            return self.async_create_entry(
                title=name,
                data={CONF_ADDRESS: info.address, CONF_NAME: name},
            )

        configured_names = self._async_current_ids()
        for info in async_discovered_service_info(self.hass):
            if not info.name or info.name in configured_names:
                continue
            if not info.name.startswith(LOCAL_NAME_PREFIX):
                continue
            # dedupe by stable name; keep the most recent advertisement
            self._discovered[info.name] = info

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(sorted(self._discovered))}
            ),
        )
