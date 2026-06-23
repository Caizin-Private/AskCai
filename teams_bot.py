from botbuilder.core import TurnContext
import logging

logger = logging.getLogger(__name__)


def _get_employee_email(turn_context: TurnContext) -> str:
    """Extract the employee's email from the Teams activity with multiple fallbacks."""
    from_prop = turn_context.activity.from_property

    if not from_prop:
        logger.warning("[email] from_property is None")
        return ""

    # Option 1: name field contains UPN / email (most common for work accounts)
    name = from_prop.name or ""
    if "@" in name:
        return name.lower().strip()

    # Option 2: id field sometimes contains the email in webchat / dev testing
    user_id = from_prop.id or ""
    if "@" in user_id:
        return user_id.lower().strip()

    # Option 3: construct email from display name as firstname.lastname@caizin.com
    if name:
        parts = name.lower().split()
        return f"{parts[0]}.{parts[-1]}@caizin.com" if len(parts) >= 2 else f"{parts[0]}@caizin.com"

    logger.warning("[email] could not resolve email from from_property")
    return ""
