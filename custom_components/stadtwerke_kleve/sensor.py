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
from .api import (
    START_URL,
    Consumption,
    ContractTerms,
    Installment,
    Payment,
    PortalData,
    Reading,
    Tariff,
)
from .const import DOMAIN

type Coordinator = DataUpdateCoordinator[PortalData]

READING_UNITS = {
    "POWER": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "GAS": (SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS),
    "WATER": (SensorDeviceClass.WATER, UnitOfVolume.CUBIC_METERS),
}
CONTRACT_DATES = (
    ("price_guarantee", "price_guarantee_until"),
    ("next_cancellation", "next_cancellation"),
)
PRICE_UNITS = {
    "POWER": f"{CURRENCY_EURO}/{UnitOfEnergy.KILO_WATT_HOUR}",
    "GAS": f"{CURRENCY_EURO}/{UnitOfEnergy.KILO_WATT_HOUR}",
    "WATER": f"{CURRENCY_EURO}/{UnitOfVolume.CUBIC_METERS}",
}


@dataclass(frozen=True, kw_only=True)
class StadtwerkeKleveSensorDescription(SensorEntityDescription):
    """Describes a sensor that reads one value from PortalData."""

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
    StadtwerkeKleveSensorDescription(
        key="unread_messages",
        translation_key="unread_messages",
        icon="mdi:email-outline",
        value_fn=lambda data: data.unread_messages,
        attrs_fn=lambda data: {
            "latest_subject": data.messages[0].subject if data.messages else None,
            "latest_date": data.messages[0].created if data.messages else None,
            "messages": [
                {"subject": m.subject, "date": m.created, "unread": m.unread}
                for m in data.messages[:10]
            ],
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StadtwerkeKleveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for the meters, contracts and prices found."""
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

    media_of_meter = {r.meter_no: r.media for r in data.readings}
    for meter_no in data.history:
        media = media_of_meter.get(meter_no)
        if media in READING_UNITS:
            suffix = f" {meter_no}" if meters[media] > 1 else ""
            entities.append(ConsumptionSensor(coordinator, entry, meter_no, media, suffix))

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

    per_media = Counter(t.media for t in data.contract_terms)
    for terms in data.contract_terms:
        if terms.media not in READING_UNITS:
            continue
        suffix = f" {terms.contract_no}" if per_media[terms.media] > 1 else ""
        for kind, attr in CONTRACT_DATES:
            if getattr(terms, attr):
                entities.append(ContractDateSensor(coordinator, entry, terms, kind, attr, suffix))

    entities += [SummarySensor(coordinator, entry, desc) for desc in SUMMARY_SENSORS]
    async_add_entities(entities)


class StadtwerkeKleveSensor(CoordinatorEntity[Coordinator], SensorEntity):
    """Base class with one device per account."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: Coordinator, entry: StadtwerkeKleveConfigEntry, key: str
    ) -> None:
        """Set unique id and device."""
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
    """Latest meter reading."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        reading: Reading,
        suffix: str,
    ) -> None:
        """Set name and unit from the media."""
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
        """Return the reading of this register from the latest data."""
        return next((r for r in self.coordinator.data.readings if r.key == self._key), None)

    @property
    def available(self) -> bool:
        """Return False once the register is no longer listed."""
        return super().available and self._reading() is not None

    @property
    def native_value(self) -> float | None:
        """Return the meter reading."""
        reading = self._reading()
        return reading.value if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return reading date, meter number and OBIS code."""
        reading = self._reading()
        if reading is None:
            return None
        return {
            "reading_date": reading.reading_date,
            "meter_number": reading.meter_no,
            "obis": reading.obis,
        }


class ConsumptionSensor(StadtwerkeKleveSensor):
    """Consumption of one meter over the last 12 months, from its reading history."""

    _unrecorded_attributes = frozenset({"readings"})

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        meter_no: str,
        media: str,
        suffix: str,
    ) -> None:
        """Set name and unit from the media."""
        super().__init__(coordinator, entry, f"consumption_{meter_no}")
        self._meter_no = meter_no
        self._attr_translation_key = f"consumption_{media.lower()}"
        self._attr_translation_placeholders = {"suffix": suffix}
        self._attr_native_unit_of_measurement = READING_UNITS[media][1]
        self._attr_suggested_display_precision = 0

    def _history(self) -> list[tuple[date, float]]:
        """Return the reading history of this meter."""
        return self.coordinator.data.history.get(self._meter_no, [])

    def _consumption(self) -> Consumption | None:
        """Return the yearly consumption, None while the history is shorter than a year."""
        return self.coordinator.data.consumption.get(self._meter_no)

    @property
    def native_value(self) -> float | None:
        """Return the consumption."""
        used = self._consumption()
        return round(used.value, 1) if used else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the period and the reading history."""
        used = self._consumption()
        if used is None:
            return None
        return {
            "period_start": used.start,
            "period_end": used.end,
            "readings": [{"date": day, "value": value} for day, value in self._history()],
        }


class PaymentSensor(StadtwerkeKleveSensor):
    """Next advance payment of one contract."""

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
        """Set name and the billing plan card of this contract."""
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
        """Return the payment of this contract from the latest data."""
        payments = self.coordinator.data.payments
        return next((p for p in payments if p.contract_no == self._contract_no), None)

    def _installments(self) -> list[Installment]:
        """Return the upcoming installments of this contract."""
        plans = self.coordinator.data.plans.get(self._media, [])
        return plans[self._plan_index] if self._plan_index < len(plans) else []

    @property
    def available(self) -> bool:
        """Return False once the contract is no longer listed."""
        return super().available and self._payment() is not None

    @property
    def native_value(self) -> float | None:
        """Return the payment amount."""
        payment = self._payment()
        return payment.amount if payment else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the upcoming installments."""
        return {
            "installments": [
                {"due": i.due, "amount": i.amount, "status": i.status} for i in self._installments()
            ]
        }


class SummarySensor(StadtwerkeKleveSensor):
    """Sensor defined by a description."""

    _unrecorded_attributes = frozenset({"messages"})

    entity_description: StadtwerkeKleveSensorDescription

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        description: StadtwerkeKleveSensorDescription,
    ) -> None:
        """Store the description."""
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | date | None:
        """Return the value from the description."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the attributes from the description."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class ContractDateSensor(StadtwerkeKleveSensor):
    """End of the price guarantee or next possible cancellation date of a contract."""

    _attr_device_class = SensorDeviceClass.DATE

    def __init__(
        self,
        coordinator: Coordinator,
        entry: StadtwerkeKleveConfigEntry,
        terms: ContractTerms,
        kind: str,
        attr: str,
        suffix: str,
    ) -> None:
        """Set name and the date field this sensor shows."""
        super().__init__(coordinator, entry, f"{kind}_{terms.contract_no}")
        self._contract_no = terms.contract_no
        self._field = attr
        self._attr_translation_key = f"{kind}_{terms.media.lower()}"
        self._attr_translation_placeholders = {"suffix": suffix}

    def _terms(self) -> ContractTerms | None:
        """Return the dates of this contract from the latest data."""
        terms = self.coordinator.data.contract_terms
        return next((t for t in terms if t.contract_no == self._contract_no), None)

    @property
    def available(self) -> bool:
        """Return False once the contract is no longer listed."""
        return super().available and self._terms() is not None

    @property
    def native_value(self) -> date | None:
        """Return the date."""
        terms = self._terms()
        return getattr(terms, self._field) if terms else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return product, contract number, start and end of the contract."""
        terms = self._terms()
        if terms is None:
            return None
        return {
            "product": terms.product,
            "contract_number": terms.contract_no,
            "contract_start": terms.start,
            "contract_end": terms.end,
        }


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
        """Set unique id and name from kind and media."""
        key = f"{self.kind}_{media.lower()}" + (f"_{index}" if index else "")
        super().__init__(coordinator, entry, key)
        self._media = media
        self._index = index
        self._attr_translation_key = f"{self.kind}_{media.lower()}"
        self._attr_translation_placeholders = {"suffix": suffix}

    def _tariff(self) -> Tariff | None:
        """Return the tariff of this contract from the latest data."""
        tariffs = [t for t in self.coordinator.data.tariffs if t.media == self._media]
        return tariffs[self._index] if self._index < len(tariffs) else None

    @property
    def available(self) -> bool:
        """Return False when no price was found."""
        return super().available and self._tariff() is not None


class EnergyPriceSensor(TariffSensor):
    """Energy price per kWh or m³."""

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
        """Set unit and precision from the media."""
        super().__init__(coordinator, entry, media, index, suffix)
        self._attr_native_unit_of_measurement = PRICE_UNITS[media]
        self._attr_suggested_display_precision = 2 if media == "WATER" else 4

    @property
    def native_value(self) -> float | None:
        """Return the energy price."""
        tariff = self._tariff()
        return tariff.energy_price if tariff else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return product, source and the prices as shown in the portal."""
        tariff = self._tariff()
        if tariff is None:
            return None
        return {
            "product": tariff.product,
            "source": tariff.source,
            "price_details": dict(tariff.details),
        }


class BasePriceSensor(TariffSensor):
    """Base price per year."""

    kind = "base_price"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        """Return the base price."""
        tariff = self._tariff()
        return round(tariff.base_price, 2) if tariff and tariff.base_price is not None else None
