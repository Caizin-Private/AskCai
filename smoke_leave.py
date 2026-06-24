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
        balances = await leave_service.get_leave_balance(TEST_EMAIL)
        if balances:
            for b in balances:
                print(f"  {b.leave_type_name}: {b.available} available / {b.total} accrued (used: {b.used})")
        else:
            print("  No balance records returned")
    except Exception as e:
        print(f"  ERROR: {e}")


asyncio.run(main())
