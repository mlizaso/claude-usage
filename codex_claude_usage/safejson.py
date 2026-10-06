"""Values that are safe to put in the JSON API.

SQLite is dynamically typed and every value here originated in a transcript, so
nothing crossing this boundary is trusted: numbers are range-checked against
what JavaScript can represent exactly, and every string passes through
`terminal_safe` before it can reach a browser.

Separated from the query layer because both the payload builder and the rollups
need it, and a shared helper living inside one of its own callers is how an
import cycle starts.
"""

import math

from .safetext import terminal_safe

# JavaScript loses integer precision above 2^53; clamp rather than ship a number
# the page would silently round.
MAX_DASHBOARD_INTEGER = (1 << 53) - 1


def safe_dashboard_value(value):
    """Escape terminal/bidi controls in every string crossing the JSON API."""
    if isinstance(value, str):
        return terminal_safe(value)
    if isinstance(value, list):
        return [safe_dashboard_value(item) for item in value]
    if isinstance(value, dict):
        return {key: safe_dashboard_value(item) for key, item in value.items()}
    return value


def dashboard_number(value, default=0):
    """Keep SQLite's dynamic typing from becoming executable HTML/invalid JSON."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return min(value, MAX_DASHBOARD_INTEGER) if value >= 0 else default
    if not isinstance(value, float) or not math.isfinite(value) or value < 0:
        return default
    return min(value, MAX_DASHBOARD_INTEGER)


def optional_dashboard_number(value):
    return dashboard_number(value, None)


def dashboard_text(value, default=""):
    return value if isinstance(value, str) else default
