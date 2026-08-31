"""
keka/dao/psa_dao.py
Raw Keka PSA (Project Services Automation) access. No business logic, no mapping.

Endpoints — https://developers.keka.com/reference/
  GET /psa/timeentries              logged time    (from, to, employeeIds; 90-day max)
  GET /psa/project/resources        which projects an employee is on (employeeIds)
  GET /psa/projects                 project catalogue
  GET /psa/projects/{id}/allocations assignment detail for one project

Requires the `Timesheet` scope on the Keka API key.
"""

import logging

from keka.dao._http import cached, get_all

logger = logging.getLogger(__name__)

# Keka rejects a from/to span wider than this on both timeentries and leaverequests.
MAX_RANGE_DAYS = 90


def fetch_time_entries(employee_id: str, from_date: str, to_date: str) -> list:
    """
    GET /psa/timeentries

    from_date/to_date are 'YYYY-MM-DD'; sent as ISO date-times because the parameter
    is typed date-time. Returns raw rows:
      {id, identifier, date, employeeId, projectId, taskId, totalMinutes,
       startTime, endTime, comments, isBillable, status}

    Note totalMinutes — minutes, not hours.
    """
    return cached(
        "time_entries",
        f"{employee_id}|{from_date}|{to_date}",
        lambda: get_all(
            "/psa/timeentries",
            {
                "employeeIds": employee_id,
                "from": f"{from_date}T00:00:00",
                "to": f"{to_date}T23:59:59",
            },
            what="GET /psa/timeentries",
        ),
    )


def fetch_allocations(employee_id: str) -> list:
    """
    GET /psa/project/resources?employeeIds=...

    Returns raw rows: {employeeId, projectId, name}

    `name` is not documented as to whether it is the project or the employee, so
    callers should resolve project names from fetch_projects() and treat this
    endpoint purely as the employee → project link.
    """
    return cached(
        "allocations",
        employee_id,
        lambda: get_all(
            "/psa/project/resources",
            {"employeeIds": employee_id},
            what="GET /psa/project/resources",
        ),
    )


def fetch_projects() -> list:
    """
    GET /psa/projects

    Tenant-wide, so cached under one key. Returns raw rows:
      {id, identifier, clientId, name, code, startDate, endDate, status,
       projectManagers, isBillable, billingType, projectBudget, budgetedTime,
       isArchived, customAttributes}
    """
    return cached(
        "projects",
        "all",
        lambda: get_all("/psa/projects", {}, what="GET /psa/projects"),
    )


def fetch_project_allocations(project_id: str) -> list:
    """
    GET /psa/projects/{id}/allocations

    Returns raw rows:
      {id, employee: {id, firstName, lastName, email},
       startDate, endDate, allocationPercentage,
       billingRole: {id, name}, billingRate: {unit, rate},
       billingType, isShadow}

    Cached **per project, not per employee** — one project's allocation list serves
    everyone on it. Three projects shared across fifty people is three cached calls,
    not a hundred and fifty. That matters against Keka's 50 requests/minute
    tenant-wide limit, because this is the one endpoint here that fans out per
    project rather than being a single call.
    """
    return cached(
        "allocations",
        f"project|{project_id}",
        lambda: get_all(
            f"/psa/projects/{project_id}/allocations",
            {},
            what=f"GET /psa/projects/{project_id}/allocations",
        ),
    )
