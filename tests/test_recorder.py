"""Recorder platform tests for statistics unit migrations."""

from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from homeassistant.const import PERCENTAGE, UnitOfVolumeFlowRate
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeFlowRateConverter
import pytest

from custom_components.resmed_myair import recorder
from custom_components.resmed_myair.models import MyAirSleepRecord

MANIFEST_PATH = Path(__file__).parents[1] / "custom_components/resmed_myair/manifest.json"


def test_manifest_loads_after_recorder_when_available() -> None:
    """Recorder support is optional but ordered before myAir when available."""
    manifest = json.loads(MANIFEST_PATH.read_text())

    assert "recorder" in manifest["after_dependencies"]
    assert "recorder" not in manifest["dependencies"]


def test_async_custom_equivalent_units_maps_mask_leak_entities(
    monkeypatch: Any,
) -> None:
    """Mask leak statistics migrate from percent to liters per minute.

    Args:
        monkeypatch (Any): Pytest patch fixture used to provide the entity registry.
    """
    registry = SimpleNamespace(
        entities={
            "sensor.cpap_mask_leak": SimpleNamespace(
                entity_id="sensor.cpap_mask_leak",
                platform="resmed_myair",
                unique_id="resmed_myair_SN123_leakPercentile",
            ),
            "sensor.cpap_usage_minutes": SimpleNamespace(
                entity_id="sensor.cpap_usage_minutes",
                platform="resmed_myair",
                unique_id="resmed_myair_SN123_totalUsage",
            ),
            "sensor.other_mask_leak": SimpleNamespace(
                entity_id="sensor.other_mask_leak",
                platform="other",
                unique_id="other_SN123_leakPercentile",
            ),
        }
    )
    monkeypatch.setattr(recorder.er, "async_get", lambda hass: registry)

    assert recorder.async_custom_equivalent_units(SimpleNamespace()) == {
        "sensor.cpap_mask_leak": {
            PERCENTAGE: UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
        },
    }


def test_async_migrate_mask_leak_statistics_metadata_updates_recorder(
    monkeypatch: Any,
) -> None:
    """Mask leak statistics metadata is relabeled to the new flow-rate unit.

    Args:
        monkeypatch (Any): Pytest patch fixture used to provide recorder and registry doubles.
    """
    registry = SimpleNamespace(
        entities={
            "sensor.cpap_mask_leak": SimpleNamespace(
                entity_id="sensor.cpap_mask_leak",
                platform="resmed_myair",
                unique_id="resmed_myair_SN123_leakPercentile",
            ),
            "sensor.cpap_usage_minutes": SimpleNamespace(
                entity_id="sensor.cpap_usage_minutes",
                platform="resmed_myair",
                unique_id="resmed_myair_SN123_totalUsage",
            ),
        }
    )
    recorder_instance = SimpleNamespace(async_update_statistics_metadata=MagicMock())
    monkeypatch.setattr(recorder.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(recorder, "get_instance", lambda hass: recorder_instance)

    recorder.async_migrate_mask_leak_statistics_metadata(
        SimpleNamespace(data={recorder.DATA_INSTANCE: recorder_instance})
    )

    recorder_instance.async_update_statistics_metadata.assert_called_once_with(
        "sensor.cpap_mask_leak",
        new_unit_class=VolumeFlowRateConverter.UNIT_CLASS,
        new_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
    )


def test_async_migrate_mask_leak_statistics_metadata_skips_without_recorder(
    monkeypatch: Any,
) -> None:
    """Mask leak metadata migration does not block setup when recorder is absent.

    Args:
        monkeypatch (Any): Pytest patch fixture used to provide a recorder lookup double.
    """
    recorder_instance = SimpleNamespace(async_update_statistics_metadata=MagicMock())
    monkeypatch.setattr(recorder, "get_instance", lambda hass: recorder_instance)

    recorder.async_migrate_mask_leak_statistics_metadata(SimpleNamespace(data={}))

    recorder_instance.async_update_statistics_metadata.assert_not_called()


def test_normalize_sleep_history_sorts_and_drops_invalid_records() -> None:
    """Sleep history normalization keeps only valid non-future records."""
    assert recorder.normalize_sleep_history(
        [
            {"startDate": "2099-01-01", "totalUsage": 60},
            {"startDate": "2026-03-02", "totalUsage": 120},
            {"startDate": "not-a-date", "totalUsage": 180},
            {"totalUsage": 240},
            {"startDate": "2026-03-01", "totalUsage": 60},
        ]
    ) == [
        {"startDate": "2026-03-01", "totalUsage": 60},
        {"startDate": "2026-03-02", "totalUsage": 120},
    ]


@pytest.mark.asyncio
async def test_async_import_historical_statistics_sorts_records_for_cumulative_sums(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Usage-hour sums are imported chronologically even if the API returns unsorted rows."""
    imported: list[dict[str, Any]] = []

    class DummyRecorder:
        async def async_add_executor_job(
            self, func: object, *args: object
        ) -> dict[str, list[dict[str, Any]]]:
            return {}

    def add_external_statistics(
        hass: HomeAssistant,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.extend(statistics)

    monkeypatch.setattr(recorder, "get_instance", lambda hass: DummyRecorder())
    monkeypatch.setattr(recorder, "async_add_external_statistics", add_external_statistics)

    await recorder.async_import_historical_statistics(
        hass,
        [
            MyAirSleepRecord.from_api({"startDate": "2026-03-03", "totalUsage": 120}),
            MyAirSleepRecord.from_api({"startDate": "2026-03-01", "totalUsage": 60}),
            MyAirSleepRecord.from_api({"startDate": "2026-03-02", "totalUsage": 30}),
        ],
        "resmed_myair:sn123_usagehours_sum",
        recorder.HistoricalStatisticDescription(
            name="CPAP Usage Hours",
            unit_of_measurement="h",
            value_fn=lambda record: recorder.usage_minutes_to_hours(record["totalUsage"]),
            import_mode="sum",
            round_digits=2,
        ),
    )

    assert [stat["start"].date().isoformat() for stat in imported] == [
        "2026-03-01",
        "2026-03-02",
        "2026-03-03",
    ]
    assert [stat["sum"] for stat in imported] == [1.0, 1.5, 3.5]


@pytest.mark.asyncio
async def test_async_import_historical_statistics_uses_local_start_date_for_resume(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume date is based on recorder row start in local time, not UTC date."""
    imported: list[dict[str, Any]] = []
    statistic_id = "resmed_myair:sn123_usagehours_sum"
    timezone = ZoneInfo("Asia/Tokyo")
    start_timestamp = datetime(2026, 3, 2, tzinfo=timezone).timestamp()

    class DummyRecorder:
        async def async_add_executor_job(
            self, func: object, *args: object
        ) -> dict[str, list[dict[str, Any]]]:
            return {statistic_id: [{"start": start_timestamp, "sum": 5.0}]}

    def add_external_statistics(
        hass: HomeAssistant,
        metadata: dict[str, Any],
        statistics: list[dict[str, Any]],
    ) -> None:
        imported.extend(statistics)

    monkeypatch.setattr(recorder, "get_instance", lambda hass: DummyRecorder())
    monkeypatch.setattr(recorder, "async_add_external_statistics", add_external_statistics)

    dt_util.set_default_time_zone(timezone)
    try:
        await recorder.async_import_historical_statistics(
            hass,
            [
                MyAirSleepRecord.from_api({"startDate": "2026-03-02", "totalUsage": 60}),
                MyAirSleepRecord.from_api({"startDate": "2026-03-03", "totalUsage": 120}),
            ],
            statistic_id,
            recorder.HistoricalStatisticDescription(
                name="CPAP Usage Hours",
                unit_of_measurement="h",
                value_fn=lambda record: recorder.usage_minutes_to_hours(record["totalUsage"]),
                import_mode="sum",
                round_digits=2,
            ),
        )
    finally:
        dt_util.set_default_time_zone(UTC)

    assert [stat["start"].date().isoformat() for stat in imported] == ["2026-03-03"]
    assert [stat["sum"] for stat in imported] == [7.0]
