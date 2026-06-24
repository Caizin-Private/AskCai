"""
keka/models.py
Typed contracts for Keka leave operations — dataclasses, enums, exceptions.
"""

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class LeaveBalance:
    leave_type_name: str
    leave_type_id: str
    total: float
    used: float
    available: float


@dataclass
class LeaveType:
    id: str
    name: str


@dataclass
class LeaveApplicationResult:
    success: bool
    message: str
    request_id: str = field(default=None)


class SessionType(str, Enum):
    FULL_DAY    = "full_day"
    FIRST_HALF  = "first_half"
    SECOND_HALF = "second_half"


class KekaServiceError(Exception):
    pass


class EmployeeNotFoundError(Exception):
    pass
