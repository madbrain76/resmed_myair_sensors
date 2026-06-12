"""Device coordinator for resmed_myair."""

from collections.abc import Mapping, Sequence
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client.myair_client import AuthenticationError, MyAirClient, ParsingError
from .const import DEFAULT_UPDATE_RATE_MIN, HISTORY_STORE_VERSION, MAX_HISTORY_DAYS
from .models import MyAirCoordinatorData, MyAirDevice, MyAirSleepRecord
from .recorder import async_get_sleep_history_from_recorder, normalize_sleep_history

_LOGGER: logging.Logger = logging.getLogger(__name__)


class MyAirDataUpdateCoordinator(DataUpdateCoordinator[MyAirCoordinatorData]):
    """Fetch and cache the typed myAir payload consumed by sensor entities."""

    myair_client: MyAirClient

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        myair_client: MyAirClient,
    ) -> None:
        """Configure periodic myAir polling for a config entry.

        Args:
            hass: Home Assistant instance running the coordinator.
            config_entry: myAir config entry associated with this coordinator.
            myair_client: Client used to authenticate and fetch myAir data.
        """
        _LOGGER.info("Initializing DataUpdateCoordinator for ResMed myAir")
        self.myair_client = myair_client
        self._serial_number: str | None = None
        self._sleep_history_store: Store[list[dict[str, Any]]] = Store(
            hass,
            HISTORY_STORE_VERSION,
            f"{config_entry.domain}_{config_entry.entry_id}_sleep_history",
        )
        self._sleep_history: list[dict[str, Any]] = []
        self._usage_hours_history: list[dict[str, Any]] = []
        super().__init__(
            hass=hass,
            config_entry=config_entry,
            logger=_LOGGER,
            name="myAir update",
            update_interval=timedelta(minutes=DEFAULT_UPDATE_RATE_MIN),
        )

    async def _async_setup(self) -> None:
        """Load persisted sleep history before the first cloud refresh."""
        stored_history = await self._sleep_history_store.async_load()
        if stored_history is None:
            self._sleep_history = []
            return
        self._sleep_history = normalize_sleep_history(stored_history)

    async def _async_update_data(self) -> MyAirCoordinatorData:
        """Refresh auth, device metadata, and recent sleep records.

        Returns:
            Typed coordinator payload containing any data available from myAir.

        Raises:
            ConfigEntryAuthFailed: When myAir authentication must be repaired by reauth.
        """
        _LOGGER.info("Updating from myAir")

        try:
            await self.myair_client.connect()
        except AuthenticationError as e:
            _LOGGER.error("Authentication Error while updating. %s: %s", type(e).__name__, e)
            raise ConfigEntryAuthFailed(
                f"Authentication Error while updating. {type(e).__name__}: {e}"
            ) from e

        device: MyAirDevice | None = None
        sleep_records: tuple[MyAirSleepRecord, ...] = ()

        try:
            device = await self.myair_client.get_user_device_data()
        except ParsingError as err:
            _LOGGER.debug(
                "Device data unavailable in myAir update. %s: %s",
                type(err).__name__,
                err,
            )
        if device and device.serial_number:
            self._serial_number = device.serial_number

        try:
            sleep_records = tuple(await self.myair_client.get_sleep_records())
        except ParsingError as err:
            _LOGGER.debug(
                "Sleep record data unavailable in myAir update. %s: %s",
                type(err).__name__,
                err,
            )

        serial_number = device.serial_number if device else self._serial_number
        self._usage_hours_history = await async_get_sleep_history_from_recorder(
            self.hass, serial_number
        )
        self._sleep_history = _merge_sleep_history(self._sleep_history, self._usage_hours_history)
        self._sleep_history = _merge_sleep_history(
            self._sleep_history, [record.raw for record in sleep_records]
        )
        await self._sleep_history_store.async_save(self._sleep_history)

        return MyAirCoordinatorData(device=device, sleep_records=sleep_records)

    @property
    def chart_sleep_records(self) -> list[dict[str, Any]]:
        """Return merged historical records for frontend chart attributes."""
        live_records = [record.raw for record in _coordinator_data_sleep_records(self.data)]
        history = self._sleep_history or live_records
        return _merge_sleep_history(self._usage_hours_history, history)


def _coordinator_data_sleep_records(data: Any) -> tuple[MyAirSleepRecord, ...]:
    """Return sleep records from a typed coordinator payload if available."""
    if isinstance(data, MyAirCoordinatorData):
        return data.sleep_records
    return ()


def _merge_sleep_history(
    existing_history: list[dict[str, Any]],
    latest_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge new cloud or recorder records into persisted sleep history."""
    merged_by_date: dict[str, dict[str, Any]] = {
        record["startDate"]: dict(record)
        for record in normalize_sleep_history(existing_history)
        if record.get("startDate")
    }
    for record in normalize_sleep_history(latest_records):
        start_date = record.get("startDate")
        if not start_date:
            continue
        merged_by_date[start_date] = {**merged_by_date.get(start_date, {}), **dict(record)}

    sorted_dates = sorted(merged_by_date)
    if len(sorted_dates) > MAX_HISTORY_DAYS:
        sorted_dates = sorted_dates[-MAX_HISTORY_DAYS:]
    return [merged_by_date[start_date] for start_date in sorted_dates]
