import os

PILOT_ALLOWLIST = {
    "maitreyee.joshi@caizin.com",
    "yash.nikam@caizin.com",
    "rohan.lande@caizin.com",
}

# Feature flags — set env var to "0" to disable for all users
_FLAGS = {
    "attendance_card": True,
    "home_tab":        True,
    "askcai_tab":      True,
    "report_popup":    True,
}


def is_pilot(email: str) -> bool:
    return (email or "").strip().lower() in PILOT_ALLOWLIST


def surface_enabled(flag: str, email: str) -> bool:
    """Return True only if the feature flag is on AND the user is in the pilot allowlist."""
    env_key = f"FEATURE_{flag.upper()}"
    flag_on = os.getenv(env_key, "1") != "0" and _FLAGS.get(flag, False)
    return flag_on and is_pilot(email)
