"""Parser checks against anonymised portal responses. Run: python tests/test_parse.py"""

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

assert api._has_login_errors('{"hasFieldErrors": true, "fielderrors": {}}')
assert not api._has_login_errors('{"hasFieldErrors": false, "hasGlobalErrors": false}')
assert not api._has_login_errors("<html>maintenance</html>")
print("ok")
