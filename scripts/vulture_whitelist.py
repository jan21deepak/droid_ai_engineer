"""Vulture whitelist for false positives.

Vulture does not honor ``# noqa`` comments, so intentionally unused names are
referenced here instead. See https://github.com/jendrikseipp/vulture#whitelists.
"""

# app/error_tracking.py: Sentry's before_send hook requires the (event, hint)
# signature even though the hint mapping is never read.
hint
