# Stadtwerke Kleve for Home Assistant

Unofficial custom integration for the [Stadtwerke Kleve customer portal](https://kundenportal.stadtwerke-kleve.de/powercommerce/csit3/fo/portal/start).
Not affiliated with Stadtwerke Kleve.

It logs in with your portal username and password and adds these sensors:

| Sensor | Unit | Notes |
|---|---|---|
| Meter reading electricity / gas / water | kWh / m³ | last reading the portal knows; attributes `reading_date`, `meter_number`, `obis` |
| Consumption last 12 months electricity / gas / water | kWh / m³ | from the reading history, interpolated to exactly one year; attributes `period_start`, `period_end`, `readings` |
| Advance payment electricity / gas / water | EUR | next payment per contract; attribute `installments` lists the upcoming ones (`due`, `amount`, `status`) |
| Total advance payment | EUR | sum of all contracts |
| Next payment due | date | |
| Price electricity / gas / water | EUR/kWh, EUR/m³ | attributes `product`, `source`, `price_details` |
| Base price per year electricity / gas / water | EUR | |
| Price guarantee until electricity / gas / water | date | only for contracts with a price guarantee; attributes `product`, `contract_number`, `contract_start`, `contract_end` |
| Next possible cancellation electricity / gas / water | date | same attributes |
| Last invoice | EUR | negative means credit; attributes `invoice_date`, `invoice_number`, `invoice_type` |

Entity names follow the Home Assistant language (German names in a German setup).

## How it works

The portal has no public API. The integration reads the same JSON endpoints
the portal's start page loads after login, plus the billing plan and contract
details pages. A portal update can break it. It polls every 6 hours.

- Meter readings are the ones on file at the Stadtwerke (annual reading or a
  reading you submitted), not live values. They can be added to the Energy
  dashboard, but each new reading shows up as one step.
- The portal only lists upcoming installments, past payments are not available.
- Water prices come from your contract. Electricity and gas prices are not
  shown for existing contracts, so they come from the portal's public price
  calculator, matched by product name. They are the current list prices of
  your product and can differ from what your contract guarantees.
- Electricity and gas prices depend on the annual consumption. By default it
  is estimated from the meter readings (gas converted with 10 kWh/m³); you
  can set your own value in kWh in the integration options.

## Example: reminder before the price guarantee ends

A notification six weeks before the electricity price guarantee runs out.
Entity ids depend on the language and area of your setup, look yours up
under Developer tools → States.

```yaml
automation:
  - alias: Price guarantee electricity ends soon
    triggers:
      - trigger: time
        at: "09:00:00"
    conditions:
      - condition: template
        value_template: >
          {% set e = 'sensor.stadtwerke_kleve_electricity_price_guarantee_until' %}
          {{ has_value(e) and (as_datetime(states(e)).date() - now().date()).days == 42 }}
    actions:
      - action: notify.notify
        data:
          message: >
            The electricity price guarantee ends on
            {{ states('sensor.stadtwerke_kleve_electricity_price_guarantee_until') }}.
            Time to compare tariffs.
```

## Installation (HACS)

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/food/ha-stadtwerke-kleve`, type *Integration*.
2. Install *Stadtwerke Kleve*, restart Home Assistant.
3. Settings → Devices & services → *Add integration* → *Stadtwerke Kleve*, enter portal username and password.

## Limitations

- Only the customer number that is active by default after login is read.
- Accounts with a one-time password on login are not supported.
- A new meter or contract only shows up after reloading the integration.

## Development

```bash
python tests/test_parse.py   # parser checks with anonymised data
python tests/live_check.py   # real login, asks for credentials
```
