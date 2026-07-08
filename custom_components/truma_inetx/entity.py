"""Base entity for the Truma iNet X (BLE) integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import TrumaCoordinator
from .truma.state import TrumaState


class TrumaEntity(CoordinatorEntity[TrumaCoordinator]):
    """Common base tying entities to the coordinator and the device registry."""

    _attr_has_entity_name = True
    # When False the entity stays available even while the BLE link is down
    # (used by the connectivity sensor, which reports that link state itself).
    _gate_on_connected = True

    def __init__(self, coordinator: TrumaCoordinator, key: str) -> None:
        """Initialize the base entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name="Truma iNet X",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def data(self) -> TrumaState:
        """Shortcut to the current Truma state."""
        return self.coordinator.data

    @property
    def available(self) -> bool:
        """Entity is available only while the BLE link reports connected."""
        if not self._gate_on_connected:
            return super().available
        return super().available and self.coordinator.data.connected
