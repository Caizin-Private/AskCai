"""
Shared fixtures and helpers for all test modules.
All fixtures use mocks — no real HTTP calls, no Keka data is touched.
"""

import pytest
import keka.client as keka_client_module


# ---------------------------------------------------------------------------
# Reset module-level caches before every test so tests are independent.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_keka_caches():
    """Clear token + employee caches before each test."""
    keka_client_module._token_cache["access_token"] = None
    keka_client_module._token_cache["expires_at"]   = 0.0
    keka_client_module._employee_cache.clear()
    yield


# ---------------------------------------------------------------------------
# Reusable mock data (matches real Keka API shapes from Postman collection)
# ---------------------------------------------------------------------------

MOCK_EMPLOYEE_ID    = "39663c6a-d8d6-49e2-aabd-a366a66fb2c2"
MOCK_EMPLOYEE_EMAIL = "recruiter@caizin.com"

MOCK_ACCESS_TOKEN = "eyJtb2NrLXRva2VuLWZvci10ZXN0aW5nfQ=="

MOCK_TOKEN_RESPONSE = {
    "access_token": MOCK_ACCESS_TOKEN,
    "expires_in":   86400,
    "token_type":   "Bearer",
    "scope":        "kekaapi",
}

MOCK_EMPLOYEES_PAGE_1 = {
    "data": [
        {
            "id":        MOCK_EMPLOYEE_ID,
            "email":     MOCK_EMPLOYEE_EMAIL,
            "firstName": "Rohan",
            "lastName":  "Lande",
        }
    ],
    "nextPage":    None,
    "succeeded":   True,
}

MOCK_LEAVE_TYPES = {
    "data": [
        {"identifier": "feb73dda-0001", "name": "Sick Leave",    "isPaid": True},
        {"identifier": "e065aa02-0002", "name": "Casual Leave",  "isPaid": True},
        {"identifier": "0479d151-0003", "name": "Earned Leave",  "isPaid": True},
        {"identifier": "e08fc6df-0004", "name": "Maternity Leave","isPaid": True},
        {"identifier": "94e4f29e-0005", "name": "Unpaid Leave",  "isPaid": False},
    ],
    "succeeded": True,
}

MOCK_LEAVE_BALANCE_RESPONSE = {
    "data": [
        {
            "employeeIdentifier": MOCK_EMPLOYEE_ID,
            "employeeNumber":     "CZ001",
            "employeeName":       "Rohan Lande",
            "leaveBalance": [
                {
                    "leaveTypeId":      "feb73dda-0001",
                    "leaveTypeName":    "Sick Leave",
                    "accruedAmount":    6,
                    "consumedAmount":   2,
                    "availableBalance": 10,
                    "annualQuota":      12,
                },
                {
                    "leaveTypeId":      "e065aa02-0002",
                    "leaveTypeName":    "Casual Leave",
                    "accruedAmount":    5,
                    "consumedAmount":   0,
                    "availableBalance": 5,
                    "annualQuota":      5,
                },
            ],
        }
    ],
    "nextPage":  None,
    "succeeded": True,
}

MOCK_LEAVE_REQUEST_PENDING = {
    "id":                 "d1e9ad56-leave-req-001",
    "employeeIdentifier": MOCK_EMPLOYEE_ID,
    "fromDate":           "2099-03-10T00:00:00Z",
    "toDate":             "2099-03-12T00:00:00Z",
    "status":             0,    # Pending
    "requestedOn":        "2099-03-01T00:00:00Z",
    "note":               "Medical appointment",
    "selection": [
        {
            "leaveTypeIdentifier": "feb73dda-0001",
            "leaveTypeName":       "Sick Leave",
            "count":               3,
        }
    ],
}

MOCK_LEAVE_REQUESTS_RESPONSE = {
    "data":      [MOCK_LEAVE_REQUEST_PENDING],
    "nextPage":  None,
    "succeeded": True,
}

MOCK_CREATE_LEAVE_SUCCESS = {
    "data":      "new-leave-req-uuid-001",
    "succeeded": True,
    "message":   "",
    "errors":    None,
}

MOCK_CREATE_LEAVE_FAILURE = {
    "data":      None,
    "succeeded": False,
    "message":   "An Error Occured",
    "errors":    ["There is not enough leave balance for Sick Leave."],
}

MOCK_CANCEL_SUCCESS = {
    "data":      True,
    "succeeded": True,
    "message":   "",
    "errors":    None,
}
