"""Parser checks against anonymised portal responses, run with python tests/test_parse.py."""

from datetime import date
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "api", pathlib.Path(__file__).parents[1] / "custom_components/stadtwerke_kleve/api.py"
)
api = sys.modules["api"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def meter(no, media, *registers, removed=""):
    """Build a meterWidget.json entry."""
    return {
        "meterNo": no,
        "mediaType": media,
        "uninstallationDate": removed,
        "lastMeterReadings": {
            f"DEFAULT_TARIFF_{obis}": {
                "meterValue": value,
                "obisCodeStr": obis,
                "readingDate": "01.10.2026",
            }
            for obis, value in registers
        },
    }


METER = {
    "model": {
        "subElements": [
            meter("M1", "POWER", ("1-1:1.8.0", "001234")),
            meter("M2", "GAS", ("7-20:3.0.0", "05678,5"), ("7-20:3.0.1", "---")),
            meter("M3", "WATER", ("8-1:1.8.0", "00321")),
            meter("OLD", "WATER", ("8-1:1.8.0", "00001"), removed="01.01.2025"),
        ]
    }
}
BUDGET = {
    "model": {
        "nextPayments": [
            {"amount": "50,00 €", "contractNo": "C1", "mediaType": "POWER"},
            {"amount": "1.095,00 â\x82¬", "contractNo": "C2", "mediaType": "GAS"},
        ],
        "subElements": [
            {
                "totalAmountSum": "1.145,00 €",
                "nextTotals": {"x": {"nextTotalMaturityDate": "01.11.2026"}},
            }
        ],
    }
}
INVOICE = {
    "model": {
        "lastInvoicesElements": [
            {"invoiceAmount": "-500,00 €", "invoiceDate": "20.01.2025", "invoiceNo": "A"},
            {"invoiceAmount": "-2,50 €", "invoiceDate": "20.01.2026", "invoiceNo": "B"},
        ]
    }
}
PLAN_PAGE = """
<h2 class="card-title">Abschlagsplan Strom-Vertrag</h2>
<table class="table table-striped"><thead><tr><th>F&auml;llig am</th><th>Betrag</th><th>Status</th></tr></thead>
<tbody>
  <tr><td class="font-bold" data-title="F&auml;llig am">
      01.11.2026
  </td><td data-title="Betrag">50,00 &euro;</td><td data-title="Status">offen</td></tr>
  <tr><td>01.12.2026</td><td>50,00 &euro;</td><td>offen</td></tr>
</tbody></table>
<h2 class="card-title">Abschlagsplan Gas-Vertrag</h2>
<table><tbody><tr><td>01.11.2026</td><td>1.095,00 &euro;</td><td>bezahlt</td></tr></tbody></table>
<h2 class="card-title">Abschlagsplan Fernw&auml;rme-Vertrag</h2>
<table><tbody><tr><td>01.11.2026</td><td>1,00 &euro;</td><td>offen</td></tr></tbody></table>
"""


def rows(plans):
    """Plans as plain tuples, for comparing."""
    return {
        m: [[(i.due, i.amount, i.status) for i in card] for card in cards]
        for m, cards in plans.items()
    }


d = api.parse(METER, BUDGET, INVOICE, PLAN_PAGE)

# Removed meters and registers without a number are skipped, decimal commas work.
assert [(r.key, r.value) for r in d.readings] == [
    ("M1_1-1:1.8.0", 1234.0),
    ("M2_7-20:3.0.0", 5678.5),
    ("M3_8-1:1.8.0", 321.0),
], d.readings
assert d.readings[0].reading_date == date(2026, 10, 1)

assert [(p.contract_no, p.amount) for p in d.payments] == [("C1", 50.0), ("C2", 1095.0)]
assert (d.payment_total, d.payment_due) == (1145.0, date(2026, 11, 1))
assert (d.invoice_amount, d.invoice_date, d.invoice_no) == (-2.5, date(2026, 1, 20), "B")

# Unknown media cards are ignored.
assert rows(d.plans) == {
    "POWER": [[(date(2026, 11, 1), 50.0, "offen"), (date(2026, 12, 1), 50.0, "offen")]],
    "GAS": [[(date(2026, 11, 1), 1095.0, "bezahlt")]],
}, d.plans

# Two contracts of the same media keep separate plans.
twice = PLAN_PAGE.replace("Gas-Vertrag", "Strom-Vertrag")
assert len(api.parse_plans(twice)["POWER"]) == 2

empty = api.parse({"model": {"subElements": []}}, {"model": {}}, {"model": {}})
assert empty.readings == [] and empty.plans == {}
assert empty.payment_total is None and empty.invoice_amount is None


def row(label, value):
    """Build a label/value row of the contract details page."""
    return (
        f'<div class="col-form-label confirm-page-label"> {label} </div></div>'
        f'<div class="col-12"><div class="col-form-label confirm-page-value" > {value} </div>'
    )


CONTRACT_PAGE = "".join(
    [
        row("Sparte", "Strom"),
        row("Produkt", "CleverStrom2030 Vertragsbindung 31.12.30"),
        row("Vertragsnummer", "C1"),
        row("Verbrauchsstelle", "Musterweg 1 12345 Musterstadt"),
        row("Sparte", "Wasser"),
        row("Produkt", "Allgemeiner Tarif Wasser"),
        row("Verbrauchsstelle", "Musterweg 1 12345 Musterstadt"),
        row(
            "Grundpreis (Verbrauch bis 600 m&sup3;/Jahr)", "2,00 &euro; / Monat je Wohnungseinheit"
        ),
        row("Arbeitspreis (Verbrauch bis 600 m&sup3;/Jahr)", "1,50 &euro; / m&sup3;"),
        row("Arbeitspreis (Verbrauch ab 601 m&sup3;/Jahr)", "1,40 &euro; / m&sup3;"),
    ]
)
contracts = api.parse_contracts(CONTRACT_PAGE)
assert [(c.media, c.contract_no, c.postal_code, c.city, len(c.prices)) for c in contracts] == [
    ("POWER", "C1", "12345", "Musterstadt", 0),
    ("WATER", "", "12345", "Musterstadt", 3),
], contracts
water = api._tariff("WATER", contracts[1].product, "contract", contracts[1].prices)
assert (water.energy_price, water.base_price) == (1.5, 24.0), water

CALCULATOR_PAGE = """
<div class="card tariff-card power recommended-tariff ">
  <h2 class="card-title"> CleverStrom2030 </h2>
  <div class="d-table-row"><div class="d-table-cell text-start"> Arbeitspreis</div>
  <div class="d-table-cell text-end"><strong> 30,00 <span class="unitStyle">ct / kWh</span></strong></div></div>
  <div class="d-table-row"><div class="d-table-cell text-start">Grundpreis</div>
  <div class="d-table-cell text-end"><strong> 90,00 <span class="unitStyle">&euro; / Jahr</span></strong></div></div>
</div>
<div class="card tariff-card power">
  <h2 class="card-title"> Allgemeiner Tarif Strom </h2>
</div>
"""
offers = api.parse_price_calculator(CALCULATOR_PAGE)
assert list(offers) == ["CleverStrom2030", "Allgemeiner Tarif Strom"], offers
rows = api.match_offer(contracts[0].product, offers)
power = api._tariff("POWER", contracts[0].product, "price_calculator", rows)
assert (power.energy_price, power.base_price) == (0.3, 90.0), power
assert api.match_offer("Unbekannt2030", offers) is None


def meter_card(meter_no, unit, *readings):
    """Build a meter card of the meter details page."""
    rows = "".join(
        f"<tr><td>\n {day}\n</td><td>\n {value} {unit}\n</td></tr>" for day, value in readings
    )
    return (
        f'<div class="card"><div class="card-header">'
        f'<h2 class="card-title">Zähler {meter_no}</h2> </div>'
        f'<table class="table table-striped 1"><thead><tr><th>Datum</th><th>Zählerstand</th></tr>'
        f"</thead><tbody>{rows}</tbody></table></div>"
    )


# Same layout as the portal's meter details page: newest reading first,
# thousands separated by a dot.
METER_PAGE = (
    '<h2 class="card-title">Zähler</h2>'
    + meter_card(
        "M1",
        "kWh",
        ("01.10.2026", "2.500"),
        ("01.04.2026", "2.000"),
        ("01.10.2025", "1.500"),
        ("01.10.2024", "100"),
    )
    + meter_card("M2", "m&sup3;", ("01.10.2026", "1.000,5"), ("01.04.2026", "900"))
    # The real page has another table after the last card (a noscript notice).
    + "<table><tr><td>01.01.2030</td><td>5 Hinweise</td></tr></table>"
)
history = api.parse_meter_history(METER_PAGE)
assert list(history) == ["M1", "M2"], history
assert history["M1"][0] == (date(2024, 10, 1), 100.0)
assert history["M2"][-1] == (date(2026, 10, 1), 1000.5)
assert len(history["M2"]) == 2, history["M2"]

used = api.consumption_last_year(history["M1"])
assert (used.value, used.start, used.end) == (1000.0, date(2025, 10, 1), date(2026, 10, 1))
# Halfway between two readings the value is interpolated.
used = api.consumption_last_year([(date(2025, 1, 1), 0.0), (date(2026, 1, 1), 365.0)])
assert used.value == 365.0
mid = [(date(2025, 1, 1), 0.0), (date(2025, 7, 2), 100.0), (date(2026, 7, 2), 200.0)]
assert api.consumption_last_year(mid).value == 100.0
# Less than a year of history gives nothing.
assert api.consumption_last_year(history["M2"]) is None
# Two registers on one day cannot be separated.
twin = [(date(2025, 1, 1), 0.0), (date(2026, 1, 1), 300.0), (date(2026, 1, 1), 100.0)]
assert api.consumption_last_year(twin) is None
# A corrected lower reading must not turn into negative consumption.
assert api.consumption_last_year([(date(2025, 1, 1), 500.0), (date(2026, 1, 1), 400.0)]) is None

# Price lookup consumption: option first, then history (gas in kWh), then default.
data = api.PortalData(
    readings=[
        api.Reading("M1_x", "POWER", "M1", "x", 2500.0, None),
        api.Reading("G1_x", "GAS", "G1", "x", 700.0, None),
    ],
    consumption={
        "M1": api.consumption_last_year(history["M1"]),
        "G1": api.consumption_last_year([(date(2025, 10, 1), 100.0), (date(2026, 10, 1), 700.0)]),
    },
)
assert api.Portal._consumption(data, {}) == {"POWER": 1000, "GAS": 6000}
assert api.Portal._consumption(data, {"GAS": 12000}) == {"POWER": 1000, "GAS": 12000}
assert api.Portal._consumption(api.PortalData(), {}) == {"POWER": 3000, "GAS": 3000}


def contract(no, media, product, start, binding="", cancel="", end=""):
    """Build a contractWidget.json entry with the fields the portal sends."""
    return {
        "contractNo": no,
        "mediaType": media,
        "product": product,
        "contractDate": "2024-05-30T00:00:00.000+02:00",
        "contractDateString": start,
        "contractBindingDate": binding,
        "nextPossibleCancellationDate": cancel,
        "contractEndDate": "",
        "contractEndDateString": end,
        "cancellationPeriod": "",
    }


CONTRACT_WIDGET = {
    "model": {
        "subElements": [
            contract("C1", "POWER", "CleverStrom2030", "30.05.2024", "31.12.2030", "31.12.2030"),
            # Measured: the general water tariff reports today for both dates.
            contract(
                "C3", "WATER", "Allgemeiner Tarif Wasser", "01.06.2024", "20.10.2026", "20.10.2026"
            ),
            contract("", "GAS", "ohne Nummer", "01.06.2024"),
            contract("C0", "POWER", "CleverStrom2023", "01.01.2023", end="29.05.2024"),
        ]
    }
}
terms = api.parse_contract_terms(CONTRACT_WIDGET, date(2026, 10, 20))
assert [t.contract_no for t in terms] == ["C1", "C3"], terms
assert (terms[0].start, terms[0].price_guarantee_until, terms[0].next_cancellation) == (
    date(2024, 5, 30),
    date(2030, 12, 31),
    date(2030, 12, 31),
)
assert (terms[1].price_guarantee_until, terms[1].next_cancellation, terms[1].end) == (
    None,
    date(2026, 10, 20),
    None,
)
assert api.parse_contract_terms({}, date(2026, 10, 20)) == []
# An ended contract that only carries the ISO field is skipped as well.
ended = contract("C9", "GAS", "Alt", "")
ended["contractEndDate"] = "2025-12-31T00:00:00.000+01:00"
assert api.parse_contract_terms({"model": {"subElements": [ended]}}, date(2026, 10, 20)) == []

assert api._has_login_errors('{"hasFieldErrors": true, "fielderrors": {}}')
assert not api._has_login_errors('{"hasFieldErrors": false, "hasGlobalErrors": false}')
assert not api._has_login_errors("<html>maintenance</html>")
print("ok")
