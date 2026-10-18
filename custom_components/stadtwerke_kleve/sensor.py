"""Sensors for meter readings, advance payments, prices and the last invoice."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from . import StadtwerkeKleveConfigEntry
from .api import START_URL, Installment, Payment, PortalData, Reading, Tariff
from .const import DOMAIN

type Coordinator = DataUpdateCoordinator[PortalData]

READING_UNITS = {
    "POWER": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "GAS": (SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS),
    "WATER": (SensorDeviceClass.WATER, UnitOfVolume.CUBIC_METERS),
}
PRICE_UNITS = {
    "POWER": f"{CURRENCY_EURO}/{UnitOfEnergy.KILO_WATT_HOUR}",
    "GAS": f"{CURRENCY_EURO}/{UnitOfEnergy.KILO_WATT_HOUR}",
    "WATER": f"{CURRENCY_EURO}/{UnitOfVolume.CUBIC_METERS}",
}


@dataclass(frozen=True, kw_only=True)
class StadtwerkeKleveSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PortalData], float | date | None]
    attrs_fn: Callable[[PortalData], dict[str, Any]] | None = None


SUMMARY_SENSORS = (
    StadtwerkeKleveSensorDescription(
        key="payment_total",
        translation_key="payment_total",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        value_fn=lambda data: data.payment_total,
    ),
    StadtwerkeKleveSensorDescription(
        key="payment_due",
        translation_key="payment_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data.payment_due,
    ),
    StadtwerkeKleveSensorDescription(
        key="invoice",
        translation_key="invoice",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        value_fn=lambda data: data.invoice_amount,
        attrs_fn=lambda data: {
            "invoice_date": data.invoice_date,
            "invoice_number": data.invoice_no,
            "invoice_type": data.invoice_type,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StadtwerkeKleveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    data = coordinator.data

    # Names only get a meter, register or contract number when a media has
    # more than one of them.
    meters = Counter(media for media, _ in {(r.media, r.meter_no) for r in data.readings})
    registers = Counter(r.meter_no for r in data.readings)
    contracts = Counter(p.media for p in data.payments)

    entities: list[SensorEntity] = []
    for reading in data.readings:
        suffix = ""
        if meters[reading.media] > 1:
            suffix += f" {reading.meter_no}"
        if registers[reading.meter_no] > 1:
            suffix += f" {reading.obis}"
        entities.append(ReadingSensor(coordinator, entry, reading, suffix))

    seen: Counter[str] = Counter()
    for payment in data.payments:
        suffix = f" {payment.contract_no}" if contracts[payment.media] > 1 else ""
        # The plan page only names the media, so contracts of the same media
        # are matched to its cards in page order.
        entities.append(PaymentSensor(coordinator, entry, payment, seen[payment.media], suffix))
        seen[payment.media] += 1

    seen.clear()
    for tariff in data.tariffs:
        if tariff.media not in PRICE_UNITS:
            continue
        index = seen[tariff.media]
        seen[tariff.media] += 1
        suffix = f" {index + 1}" if index else ""
        entities += [
            EnergyPriceSensor(coordinator, entry, tariff.media, index, suffix),
            BasePriceSensor(coordinator, entry, tariff.media, index, suffix),
        ]

    entities += [SummarySensor(coordinator, entry, desc) for desc in SUMMARY_SENSORS]
    async_add_entities(entities)


class StadtwerkeKleveSensor(CoordinatorEntity[Coordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: Coordinator, entry: StadtwerkeKleveConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Stadtwerke Kleve",
            manufacturer="Stadtwerke Kleve",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=START_URL,
        )


class ReadingSensor(StadtwerkeKleveSensor):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        reading: Reading,
        suffix: str,
    ) -> None:
        super().__init__(coordinator, entry, f"reading_{reading.key}")
        self._key = reading.key
        if reading.media in READING_UNITS:
            self._attr_translation_key = f"reading_{reading.media.lower()}"
            self._attr_translation_placeholders = {"suffix": suffix}
            self._attr_device_class, self._attr_native_unit_of_measurement = READING_UNITS[
                reading.media
            ]
        else:
            self._attr_name = f"{reading.media.title()}{suffix}"

    def _reading(self) -> Reading | None:
        return next((r for r in self.coordinator.data.readings if r.key == self._key), None)

    @property
    def available(self) -> bool:
        return super().available and self._reading() is not None

    @property
    def native_value(self) -> float | None:
        reading = self._reading()
        return reading.value if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        reading = self._reading()
        if reading is None:
            return None
        return {
            "reading_date": reading.reading_date,
            "meter_number": reading.meter_no,
            "obis": reading.obis,
        }


class PaymentSensor(StadtwerkeKleveSensor):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_suggested_display_precision = 2
    _unrecorded_attributes = frozenset({"installments"})

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        payment: Payment,
        plan_index: int,
        suffix: str,
    ) -> None:
        super().__init__(coordinator, entry, f"payment_{payment.contract_no}")
        self._contract_no = payment.contract_no
        self._media = payment.media
        self._plan_index = plan_index
        if payment.media in READING_UNITS:
            self._attr_translation_key = f"payment_{payment.media.lower()}"
            self._attr_translation_placeholders = {"suffix": suffix}
        else:
            self._attr_name = f"{payment.media.title()}{suffix}"

    def _payment(self) -> Payment | None:
        payments = self.coordinator.data.payments
        return next((p for p in payments if p.contract_no == self._contract_no), None)

    def _installments(self) -> list[Installment]:
        plans = self.coordinator.data.plans.get(self._media, [])
        return plans[self._plan_index] if self._plan_index < len(plans) else []

    @property
    def available(self) -> bool:
        return super().available and self._payment() is not None

    @property
    def native_value(self) -> float | None:
        payment = self._payment()
        return payment.amount if payment else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "installments": [
                {"due": i.due, "amount": i.amount, "status": i.status} for i in self._installments()
            ]
        }


class SummarySensor(StadtwerkeKleveSensor):
    entity_description: StadtwerkeKleveSensorDescription

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        description: StadtwerkeKleveSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | date | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class TariffSensor(StadtwerkeKleveSensor):
    """Base for the price sensors of one contract."""

    kind: str

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        media: str,
        index: int,
        suffix: str,
    ) -> None:
        key = f"{self.kind}_{media.lower()}" + (f"_{index}" if index else "")
        super().__init__(coordinator, entry, key)
        self._media = media
        self._index = index
        self._attr_translation_key = f"{self.kind}_{media.lower()}"
        self._attr_translation_placeholders = {"suffix": suffix}

    def _tariff(self) -> Tariff | None:
        tariffs = [t for t in self.coordinator.data.tariffs if t.media == self._media]
        return tariffs[self._index] if self._index < len(tariffs) else None

    @property
    def available(self) -> bool:
        return super().available and self._tariff() is not None


class EnergyPriceSensor(TariffSensor):
    kind = "energy_price"
    _unrecorded_attributes = frozenset({"price_details"})

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        media: str,
        index: int,
        suffix: str,
    ) -> None:
        super().__init__(coordinator, entry, media, index, suffix)
        self._attr_native_unit_of_measurement = PRICE_UNITS[media]
        self._attr_suggested_display_precision = 2 if media == "WATER" else 4

    @property
    def native_value(self) -> float | None:
        tariff = self._tariff()
        return tariff.energy_price if tariff else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        tariff = self._tariff()
        if tariff is None:
            return None
        return {
            "product": tariff.product,
            "source": tariff.source,
            "price_details": dict(tariff.details),
        }


class BasePriceSensor(TariffSensor):
    kind = "base_price"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        tariff = self._tariff()
        return round(tariff.base_price, 2) if tariff and tariff.base_price is not None else None
