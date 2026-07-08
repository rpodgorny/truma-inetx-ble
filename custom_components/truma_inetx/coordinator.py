"""Coordinator for the Truma iNet X (BLE) integration.

Stage 1 (scaffolding): the coordinator owns the shared :class:`TrumaState` and
the device address, and exposes the lifecycle/command surface that the entity
platforms bind to. The live BLE transport (connect, register identity,
subscribe, parse notifications, write params) is added in stage 2 — the entity
and platform layer above this does not change when that lands.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, LOGGER
from .truma.state import TrumaState

type TrumaConfigEntry = ConfigEntry[TrumaCoordinator]


class TrumaCoordinator(DataUpdateCoordinator[TrumaState]):
    """Hold Truma state and mediate commands between HA and the BLE transport."""

    config_entry: TrumaConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: TrumaConfigEntry, address: str
    ) -> None:
        """Initialize the coordinator (push model, no polling interval)."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {address}",
            update_interval=None,
        )
        self.address = address
        self._state = TrumaState()

    async def _async_update_data(self) -> TrumaState:
        """Return the current shared state (updated by BLE notifications)."""
        return self._state

    @property
    def state_obj(self) -> TrumaState:
        """The live TrumaState instance shared with the transport."""
        return self._state

    async def async_start(self) -> None:
        """Start the BLE transport. No-op until stage 2 wires the transport."""
        LOGGER.debug("truma_inetx: coordinator start (transport not yet wired)")

    async def async_stop(self) -> None:
        """Stop the BLE transport and release the connection."""
        LOGGER.debug("truma_inetx: coordinator stop")

    async def async_write(self, topic: str, param: str, value: int) -> None:
        """Validate and send a parameter write to the panel/heater.

        Raises until the BLE transport is wired in stage 2, so that entity
        commands surface a clear error instead of silently no-op'ing.
        """
        ok, msg = TrumaState.validate_command(topic, param, value)
        if not ok:
            raise HomeAssistantError(f"Invalid Truma command: {msg}")
        raise HomeAssistantError(
            "Truma BLE control is not available yet — transport is wired in "
            "the next stage (scaffolding build)."
        )
