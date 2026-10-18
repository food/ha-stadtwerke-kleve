"""Log in for real and print what the integration would see.

Run: python tests/live_check.py [--wrong-password]

--wrong-password first tries one login with a wrong password to check that
it is reported as AuthError. Do not use it often, the portal may lock the
account after repeated failures.
"""

import asyncio
import getpass
import importlib.util
import pathlib
import sys

import aiohttp

spec = importlib.util.spec_from_file_location(
    "api", pathlib.Path(__file__).parents[1] / "custom_components/stadtwerke_kleve/api.py"
)
api = sys.modules["api"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


async def main() -> None:
    """Log in, fetch everything and print it."""
    user, password = input("Username: ").strip(), getpass.getpass("Password: ")
    async with aiohttp.ClientSession() as session:
        if "--wrong-password" in sys.argv:
            try:
                await api.Portal(session, user, password + "-wrong").login()
                print("FAIL: wrong password was accepted")
            except api.AuthError:
                print("wrong password -> AuthError, ok")
        data = await api.Portal(session, user, password).fetch()

    for r in data.readings:
        print(f"{r.media:6} {r.obis:12} {r.value:>10} on {r.reading_date}")
    for p in data.payments:
        print(f"payment {p.media:6} {p.amount:8.2f} EUR")
    print(f"payment total {data.payment_total} EUR, due {data.payment_due}")
    for media, cards in data.plans.items():
        for card in cards:
            print(f"plan {media:6}", ", ".join(f"{i.due} {i.amount} {i.status}" for i in card))
    print(f"last invoice {data.invoice_amount} EUR on {data.invoice_date} ({data.invoice_type})")
    for meter_no, readings in data.history.items():
        used = data.consumption.get(meter_no)
        print(
            f"history {meter_no}: {len(readings)} readings,",
            f"last year {used.value:.1f} ({used.start} to {used.end})"
            if used
            else "no yearly value",
        )
    for t in data.tariffs:
        print(
            f"tariff {t.media:6} {t.energy_price} EUR/unit, {t.base_price} EUR/year, {t.source}: {t.product}"
        )


asyncio.run(main())
