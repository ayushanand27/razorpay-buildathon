"""
Optional Groq API key rotation. Groq's free tier is 1,000 requests/day
PER KEY, shared across upsell_copy.py and nlu.py -- easy to exhaust
during heavy testing or a long demo session with lots of free-text
chat turns. If you have a backup key, set GROQ_API_KEY_2 (and _3, _4,
...) in .env; a 429 (rate limit) on one key is retried against the
next automatically, before falling back to the static/fixed-hint
behavior either module already has. With no backup keys configured,
nothing about existing behavior changes -- this is purely additive.
"""

import os


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
    different key). Returns None if no key was configured at all."""
    keys = [k for k in [primary_key, *backup_keys()] if k]
    if not keys:
        return None

    last_resp = None
    for key in keys:
        resp = post_fn(key, *args, **kwargs)
        last_resp = resp
        if resp.status_code != 429:
            return resp
    return last_resp
