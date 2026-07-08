"""The Truma iNet X (BLE) integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import LOGGER
from .coordinator import TrumaConfigEntry, TrumaCoordinator

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BINARY_SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: TrumaConfigEntry) -> bool:
    """Set up Truma iNet X from a config entry."""
    address: str = entry.data[CONF_ADDRESS].upper()

    # Not fatal in the scaffolding stage: if the panel is not advertising right
    # now the entities still register (as unavailable). Stage 2 will treat a
    # missing connectable device as ConfigEntryNotReady and retry.
    if bluetooth.async_ble_device_from_address(hass, address, True) is None:
        LOGGER.warning(
            "Truma panel %s not currently reachable over BLE; entities will be "
            "unavailable until it is in range",
            address,
        )

    coordinator = TrumaCoordinator(hass, entry, address)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TrumaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
