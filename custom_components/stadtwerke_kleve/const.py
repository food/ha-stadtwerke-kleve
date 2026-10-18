"""Constants for the Stadtwerke Kleve integration."""

from datetime import timedelta

DOMAIN = "stadtwerke_kleve"

# Readings and payments change a few times a year at most.
UPDATE_INTERVAL = timedelta(hours=6)

# Annual consumption in kWh, used to look up the price tier.
CONF_CONSUMPTION_POWER = "consumption_power"
CONF_CONSUMPTION_GAS = "consumption_gas"
