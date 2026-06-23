import os

_env_list = os.getenv("PILOT_ALLOWLIST", "")
PILOT_ALLOWLIST = (
    {e.strip().lower() for e in _env_list.split(",") if e.strip()}
    if _env_list
    else {
        "maitreyee.joshi@caizin.com",
        "yash.nikam@caizin.com",
        "rohan.lande@caizin.com",
        "nikhil.negi@caizin.com",
    }
)

# Surface flags — set FEATURE_<FLAG_UPPER>=0 to disable for all users
_FLAGS = {
    "attendance_card": True,
    "askcai_tab":      True,
}

# Maps each compose-extension command to its surface flag.
# Commands not listed here are always available (e.g. help).
COMMAND_FLAGS = {
    "balance":    "attendance_card",
    "applyLeave": "attendance_card",
    "attendance": "attendance_card",
}


def is_pilot(email: str) -> bool:
    return (email or "").strip().lower() in PILOT_ALLOWLIST


def surface_enabled(flag: str, email: str) -> bool:
    """True only if the feature flag is on AND the user is in the pilot allowlist."""
    flag_on = os.getenv(f"FEATURE_{flag.upper()}", "1") != "0" and _FLAGS.get(flag, False)
    return flag_on and is_pilot(email)
