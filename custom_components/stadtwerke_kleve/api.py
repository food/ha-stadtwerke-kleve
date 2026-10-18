"""Client for the Stadtwerke Kleve customer portal (powercommerce).

The portal has no public API. Its start page loads its tiles from JSON
endpoints after a session login; this client uses exactly those. The
billing plan and the contract details only exist as HTML pages.

Prices for electricity and gas are not shown for existing contracts. They
come from the portal's public price calculator, matched by product name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import html
from itertools import pairwise
import json
import logging
import re

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE = "https://kundenportal.stadtwerke-kleve.de/powercommerce/csit3/fo/portal/"
START_URL = BASE + "start"
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
TIMEOUT = aiohttp.ClientTimeout(total=30)

# Media names as the portal writes them in page titles and labels.
MEDIA_TITLES = {"Strom": "POWER", "Gas": "GAS", "Wasser": "WATER"}

# Annual consumption in kWh sent to the price calculator when neither an
# option nor the meter history gives one. Gas prices are tiered by
# consumption, this picks the lowest tier.
DEFAULT_CONSUMPTION = 3000

# Gas is billed in kWh but metered in m³. The exact factor (calorific value
# times correction factor) is on the annual bill; for picking a price tier a
# typical value is close enough.
GAS_KWH_PER_M3 = 10.0


class AuthError(Exception):
    """The portal rejected username or password."""


class PortalError(Exception):
    """The portal is unreachable or answered with something unexpected."""


@dataclass
class Reading:
    key: str  # meter number + OBIS code, stable across polls
    media: str  # POWER, GAS or WATER
    meter_no: str
    obis: str
    value: float
    reading_date: date | None


@dataclass
class Payment:
    contract_no: str
    media: str
    amount: float


@dataclass
class Installment:
    due: date
    amount: float | None
    status: str


@dataclass
class Tariff:
    media: str
    product: str
    energy_price: float | None  # EUR per kWh, or per m³ for water
    base_price: float | None  # EUR per year
    source: str  # "contract" or "price_calculator"
    details: list[tuple[str, str]] = field(default_factory=list)  # as shown in the portal


@dataclass
class Contract:
    media: str
    product: str = ""
    contract_no: str = ""
    postal_code: str | None = None
    city: str | None = None
    prices: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class PortalData:
    readings: list[Reading] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    # One installment list per contract, grouped by media in page order.
    plans: dict[str, list[list[Installment]]] = field(default_factory=dict)
    payment_total: float | None = None
    payment_due: date | None = None
    invoice_amount: float | None = None
    invoice_date: date | None = None
    invoice_no: str | None = None
    invoice_type: str | None = None
    tariffs: list[Tariff] = field(default_factory=list)
    # Reading history per meter number, oldest first.
    history: dict[str, list[tuple[date, float]]] = field(default_factory=dict)
    # Consumption over the last year per meter number, from the history.
    consumption: dict[str, Consumption] = field(default_factory=dict)


@dataclass
class Consumption:
    value: float
    start: date
    end: date


def _amount(text: str | None) -> float | None:
    # "-1.234,56 €" -> -1234.56. Only the digits are matched because the
    # € sign arrives in varying encodings.
    m = re.search(r"-?[\d.]+,\d{2}", text or "")
    return float(m.group(0).replace(".", "").replace(",", ".")) if m else None


def _price(text: str | None) -> float | None:
    """EUR value of a price like "33,44 ct / kWh" or "1,97 € / Monat"."""
    m = re.search(r"\d[\d.]*,\d+", text or "")
    if not m:
        return None
    value = float(m.group(0).replace(".", "").replace(",", "."))
    return value / 100 if "ct" in (text or "") else value


def _number(text: str | None) -> float | None:
    try:
        return float((text or "").strip().replace(",", "."))
    except ValueError:
        return None


def _german_number(text: str | None) -> float | None:
    # "4.638 kWh" -> 4638.0, "1.234,5 m³" -> 1234.5
    m = re.search(r"\d[\d.]*(?:,\d+)?", text or "")
    return float(m.group(0).replace(".", "").replace(",", ".")) if m else None


def _date(text: str | None) -> date | None:
    try:
        return datetime.strptime((text or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _text(fragment: str) -> str:
    plain = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return " ".join(plain.split())


def parse_plans(page: str) -> dict[str, list[list[Installment]]]:
    """Read the installment tables from the billing plan details page.

    The page has one card per contract, each with a table of due date,
    amount and status.
    """
    plans: dict[str, list[list[Installment]]] = {}
    for card in re.split(r"<h2[^>]*>\s*Abschlagsplan\s+", page)[1:]:
        media = MEDIA_TITLES.get(card.split("-", 1)[0].strip())
        if media is None:
            continue
        rows = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", card, re.S):
            cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) >= 3 and (due := _date(cells[0])):
                rows.append(Installment(due, _amount(cells[1]), cells[2]))
        plans.setdefault(media, []).append(rows)
    return plans


def parse_meter_history(page: str) -> dict[str, list[tuple[date, float]]]:
    """Read the reading history of every meter from the meter details page.

    Each meter has a card titled "Zähler <number>" with a table of reading
    date and value. Only that first table counts: the last card would
    otherwise run on into whatever the page has below it.
    """
    history: dict[str, list[tuple[date, float]]] = {}
    cards = re.split(r'<h2 class="card-title">\s*Z\S{1,8}hler\s+', page)[1:]
    for card in cards:
        meter_no = card.split("<", 1)[0].strip()
        table = card.split("</table>", 1)[0]
        rows = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
            cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) >= 2 and (day := _date(cells[0])):
                value = _german_number(cells[1])
                if value is not None:
                    rows.append((day, value))
        if meter_no and rows:
            history[meter_no] = sorted(rows)
    return history


def consumption_last_year(readings: list[tuple[date, float]]) -> Consumption | None:
    """Consumption over the 365 days before the latest reading.

    Readings are irregular, so the value one year back is interpolated
    linearly between the two readings around it. Returns None when the
    history cannot give a sensible value.
    """
    if len(readings) < 2:
        return None
    # Two rows on one day means a meter with several registers (HT/NT) whose
    # rows cannot be told apart here.
    if len({day for day, _ in readings}) != len(readings):
        return None
    end, end_value = readings[-1]
    start = end - timedelta(days=365)
    for (day_a, value_a), (day_b, value_b) in pairwise(readings):
        if day_a <= start <= day_b:
            share = (start - day_a).days / max((day_b - day_a).days, 1)
            used = end_value - (value_a + share * (value_b - value_a))
            # A corrected lower reading or a replaced counter runs backwards.
            return Consumption(used, start, end) if used >= 0 else None
    return None


def parse_contracts(page: str) -> list[Contract]:
    """Read the contracts from the contract details page.

    Each contract is a run of label/value rows that starts with "Sparte".
    Only some contracts (water) list their prices there.
    """
    contracts: list[Contract] = []
    rows = re.findall(
        r'confirm-page-label"\s*>(.*?)</div>.*?confirm-page-value"\s*>(.*?)</div>', page, re.S
    )
    for raw_label, raw_value in rows:
        label, value = _text(raw_label), _text(raw_value)
        if label == "Sparte":
            contracts.append(Contract(MEDIA_TITLES.get(value, value.upper())))
        elif not contracts:
            continue
        elif label == "Produkt":
            contracts[-1].product = value
        elif label == "Vertragsnummer":
            contracts[-1].contract_no = value
        elif label == "Verbrauchsstelle":
            if m := re.search(r"\b(\d{5})\s+(.+)$", value):
                contracts[-1].postal_code, contracts[-1].city = m.groups()
        elif "preis" in label.lower():
            contracts[-1].prices.append((label, value))
    return contracts


def _tariff(media: str, product: str, source: str, prices: list[tuple[str, str]]) -> Tariff:
    # With tiered prices the first tier counts.
    energy = next((v for k, v in prices if k.startswith("Arbeitspreis")), None)
    base = next((v for k, v in prices if k.startswith("Grundpreis")), None)
    base_price = _price(base)
    if base_price is not None and "Monat" in (base or ""):
        base_price *= 12
    return Tariff(media, product, _price(energy), base_price, source, prices)


def parse_price_calculator(page: str) -> dict[str, list[tuple[str, str]]]:
    """Read the tariff cards of the public price calculator.

    Returns the price rows (label, value) of each card by product name.
    """
    offers: dict[str, list[tuple[str, str]]] = {}
    for card in re.split(r'<div class="card tariff-card', page)[1:]:
        title = re.search(r'<h2 class="card-title">(.*?)</h2>', card, re.S)
        if title is None:
            continue
        rows = re.findall(
            r'd-table-cell text-start">(.*?)</div>\s*<div class="d-table-cell text-end">(.*?)</div>',
            card,
            re.S,
        )
        offers.setdefault(_text(title.group(1)), [(_text(k), _text(v)) for k, v in rows[:2]])
    return offers


def match_offer(
    product: str, offers: dict[str, list[tuple[str, str]]]
) -> list[tuple[str, str]] | None:
    """Find the calculator offer for a contract product.

    Contract products carry a suffix ("CleverGas2026 Haushalt VB 31.12.26"),
    the calculator a different one ("CleverGas2026 mit Preisgarantie*"), so
    only the first word is compared.
    """
    word = product.split(" ", 1)[0]
    return next(
        (rows for name, rows in offers.items() if word and name.split(" ", 1)[0] == word), None
    )


def parse(meter: dict, budget: dict, invoice: dict, plan_page: str = "") -> PortalData:
    data = PortalData(plans=parse_plans(plan_page))

    for m in meter["model"]["subElements"]:
        if m.get("uninstallationDate"):
            continue
        for reg in (m.get("lastMeterReadings") or {}).values():
            value = _number(reg.get("meterValue"))
            if value is None:
                _LOGGER.debug(
                    "Skipping register %s without a numeric value", reg.get("obisCodeStr")
                )
                continue
            data.readings.append(
                Reading(
                    key=f"{m['meterNo']}_{reg['obisCodeStr']}",
                    media=m["mediaType"],
                    meter_no=m["meterNo"],
                    obis=reg["obisCodeStr"],
                    value=value,
                    reading_date=_date(reg.get("readingDate")),
                )
            )

    model = budget["model"]
    for p in model.get("nextPayments") or []:
        if (amount := _amount(p.get("amount"))) is not None:
            data.payments.append(Payment(p["contractNo"], p["mediaType"], amount))
    # One billing plan per customer number, so the first entry is the only one.
    if subs := model.get("subElements"):
        data.payment_total = _amount(subs[0].get("totalAmountSum"))
        totals = list((subs[0].get("nextTotals") or {}).values())
        if totals:
            data.payment_due = _date(totals[0].get("nextTotalMaturityDate"))

    invoices = invoice["model"].get("lastInvoicesElements") or []
    if invoices:
        last = max(invoices, key=lambda i: _date(i.get("invoiceDate")) or date.min)
        data.invoice_amount = _amount(last.get("invoiceAmount"))
        data.invoice_date = _date(last.get("invoiceDate"))
        data.invoice_no = last.get("invoiceNo")
        data.invoice_type = last.get("invoiceTypeStr")
    return data


def _has_login_errors(body: str) -> bool:
    try:
        reply = json.loads(body)
    except ValueError:
        return False
    return isinstance(reply, dict) and bool(
        reply.get("hasFieldErrors") or reply.get("hasGlobalErrors")
    )


class Portal:
    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._creds = {"login": username, "password": password, "oneTimePassword": ""}

    async def _fetch_text(self, path: str, headers: dict | None = None) -> str:
        async with self._session.get(BASE + path, headers=headers, timeout=TIMEOUT) as r:
            if r.status != 200:
                raise PortalError(f"{path}: HTTP {r.status}")
            return (await r.read()).decode("utf-8", "replace")

    async def _fetch_json(self, path: str) -> dict:
        body = await self._fetch_text(path, XHR)
        try:
            return json.loads(body)
        except ValueError as err:
            # The portal answers with its login page once the session is gone.
            raise PortalError(f"{path}: no JSON") from err

    async def login(self) -> None:
        self._session.cookie_jar.clear()
        try:
            await self._fetch_text("start")
            async with self._session.post(
                BASE + "loginWidget.json", data=self._creds, headers=XHR, timeout=TIMEOUT
            ) as r:
                body = await r.text(errors="replace")
                if r.status != 200:
                    if _has_login_errors(body):
                        raise AuthError
                    raise PortalError(f"loginWidget.json: HTTP {r.status}")
            async with self._session.post(
                BASE + "loginProcess", data=self._creds, timeout=TIMEOUT
            ) as r:
                if r.status != 200:
                    raise PortalError(f"loginProcess: HTTP {r.status}")
                page = await r.text(errors="replace")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PortalError(str(err) or type(err).__name__) from err

        if "logout" in page:
            return
        if 'id="loginProcessForm"' in page:
            # Back on the login form: the credentials were not accepted.
            raise AuthError
        raise PortalError("login: unexpected page, maybe maintenance")

    async def _price_calculator(
        self, media: str, postal_code: str, city: str, consumption: int
    ) -> str:
        """Run the public price calculator and return its result page.

        This works without login. It clears the cookies, so call it last.
        """
        form = {
            "customerType": "PRIVATE",
            "mediaType": media,
            "postalCode": postal_code,
            "city": city,
            "consumption": str(consumption),
        }
        self._session.cookie_jar.clear()
        await self._fetch_text("start")
        async with self._session.post(
            BASE + "productSearchWidgetV3.json", data=form, headers=XHR, timeout=TIMEOUT
        ) as r:
            if r.status != 200:
                raise PortalError(f"productSearchWidgetV3.json: HTTP {r.status}")
        async with self._session.post(BASE + "productSearch", data=form, timeout=TIMEOUT) as r:
            if r.status != 200:
                raise PortalError(f"productSearch: HTTP {r.status}")
            return (await r.read()).decode("utf-8", "replace")

    @staticmethod
    def _consumption(data: PortalData, overrides: Mapping[str, int]) -> dict[str, int]:
        """Yearly kWh per media for the price lookup: option, then history, then default."""
        result = {}
        for media in ("POWER", "GAS"):
            if overrides.get(media):
                result[media] = overrides[media]
                continue
            meters = {r.meter_no for r in data.readings if r.media == media}
            total = sum(data.consumption[m].value for m in meters if m in data.consumption)
            if media == "GAS":
                total *= GAS_KWH_PER_M3
            result[media] = round(total) or DEFAULT_CONSUMPTION
        return result

    async def _tariffs(self, consumption: Mapping[str, int]) -> list[Tariff]:
        tariffs = []
        for contract in parse_contracts(await self._fetch_text("contractDetails")):
            if contract.prices:
                tariffs.append(
                    _tariff(contract.media, contract.product, "contract", contract.prices)
                )
            elif contract.postal_code and contract.media in ("POWER", "GAS"):
                page = await self._price_calculator(
                    contract.media,
                    contract.postal_code,
                    contract.city or "",
                    consumption[contract.media],
                )
                if rows := match_offer(contract.product, parse_price_calculator(page)):
                    tariffs.append(
                        _tariff(contract.media, contract.product, "price_calculator", rows)
                    )
        return tariffs

    async def _optional_page(self, path: str) -> str:
        """Fetch a page whose data is extra; keep the other sensors alive without it."""
        try:
            return await self._fetch_text(path)
        except (PortalError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("%s not available: %s", path, err)
            return ""

    async def fetch(self, consumption: Mapping[str, int] | None = None) -> PortalData:
        """Read everything.

        consumption maps POWER/GAS to kWh per year for the price lookup. Media
        not in it are estimated from the meter history.
        """
        # A fresh login on every poll. Polls are hours apart, the portal
        # session would have expired in between anyway.
        await self.login()
        try:
            meter = await self._fetch_json("meterWidget.json?responsiveDesign=true")
            budget = await self._fetch_json("budgetBillingPlanWidget.json")
            invoice = await self._fetch_json("invoiceWidget.json")
            data = parse(
                meter, budget, invoice, await self._optional_page("budgetBillingPlanDetails")
            )
            data.history = parse_meter_history(await self._optional_page("meterDetails"))
            data.consumption = {
                meter_no: used
                for meter_no, readings in data.history.items()
                if (used := consumption_last_year(readings))
            }
            try:
                # Last, because the price calculator drops the login session.
                data.tariffs = await self._tariffs(self._consumption(data, consumption or {}))
            except (PortalError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Prices not available: %s", err)
            return data
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PortalError(str(err) or type(err).__name__) from err
        except (KeyError, TypeError, AttributeError) as err:
            raise PortalError(f"Unexpected portal data: {err!r}") from err
