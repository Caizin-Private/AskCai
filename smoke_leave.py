import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from keka.leave_service import leave_service

TEST_EMAIL = os.getenv("KEKA_TEST_EMAIL", "rohan.lande@caizin.com")


async def main():
    print(f"Testing with email: {TEST_EMAIL}\n")

    print("=== Leave Types ===")
    try:
        types = await leave_service.get_leave_types()
        for lt in types:
            print(f"  {lt.id}  {lt.name}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n=== Leave Balance ===")
    try:
        emp_name, balances = await leave_service.get_leave_balance(TEST_EMAIL)
        print(f"  {emp_name}  ({TEST_EMAIL})")
        print(f"  {'Leave Type':<28} {'Accrued':>8} {'Used':>6} {'Available':>10}")
        print(f"  {'-'*56}")
        for b in balances:
            print(f"  {b.leave_type_name:<28} {b.total:>8.1f} {b.used:>6.1f} {b.available:>10.1f}")
    except Exception as e:
        print(f"  ERROR: {e}")


asyncio.run(main())
