"""
Optional Groq API key rotation. Groq's free tier is 1,000 requests/day
PER KEY, shared across upsell_copy.py and nlu.py -- easy to exhaust
during heavy testing or a long demo session with lots of free-text
chat turns. If you have a backup key, set GROQ_API_KEY_2 (and _3, _4,
...) in .env; a 429 (rate limit) on one key is retried against the
next automatically, before falling back to the static/fixed-hint
behavior either module already has. With no backup keys configured,
nothing about existing behavior changes -- this is purely additive.

Total quota exhaustion (every configured key came back 429) is a
DISTINCT, typed failure (QuotaExhaustedError), not just "some other
Response object" for the caller to inspect a status code on -- a
caller that wants to tell "we're out of quota" apart from "Groq's API
had a network blip" (nlu.py's StaticFallbackNLU does exactly that)
needs a signal it can catch specifically, not a return value that
looks the same either way. This module also logs the exhaustion itself
(once, here, at the single chokepoint both callers share) so it's
visible in ordinary application logs/monitoring even for a caller that
only cares about the end result, not the reason.
"""

import logging
import os

logger = logging.getLogger(__name__)


class QuotaExhaustedError(Exception):
    """Raised when EVERY configured Groq key (primary + all backups)
    returned 429 for this call. Callers that want to distinguish total
    quota exhaustion from a transient network/response error (rather
    than silently falling back the same way for both) should catch
    this specifically."""

    def __init__(self, keys_tried: int):
        self.keys_tried = keys_tried
        super().__init__(f"all {keys_tried} configured Groq key(s) returned 429 (quota exhausted)")


def backup_keys() -> list[str]:
    keys = []
    i = 2
    while True:
        key = os.environ.get(f"GROQ_API_KEY_{i}", "").strip()
        if not key:
            break
        keys.append(key)
        i += 1
    return keys


def post_with_rotation(post_fn, primary_key: str, *args, **kwargs):
    """Calls post_fn(key, *args, **kwargs) -> requests.Response for the
    primary key (if set), then each backup key in turn, stopping at
    the first non-429 response (success, or a real error worth
    surfacing as-is -- only "rate limit exhausted" warrants trying a
    different key).

    Returns None if no key was configured at all (nothing to call).
    Raises QuotaExhaustedError if at least one key was configured and
    EVERY one of them came back 429 -- this is an explicit, monitored
    failure mode, not a value indistinguishable from "no key set"."""
    keys = [k for k in [primary_key, *backup_keys()] if k]
    if not keys:
        return None

    for key in keys:
        resp = post_fn(key, *args, **kwargs)
        if resp.status_code != 429:
            return resp

    logger.warning("groq_quota_exhausted: all %d configured Groq key(s) returned 429", len(keys))
    raise QuotaExhaustedError(len(keys))
