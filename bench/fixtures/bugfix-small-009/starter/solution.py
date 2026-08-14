"""BUGGY STARTER for bugfix-small-009.

Bugs (each maps to a check.sh case):
1. NO backoff delay between retries -> retries hammer the failing service.
2. retryable filter IGNORED -> non-retryable exceptions are retried.
3. Returns None on final failure instead of raising the last exception
   (silent success = ships broken work; model-or-fail honesty violation).
4. attempts=0/negative not validated -> silent no-op instead of ValueError.
5. jitter accepted but ignored.
"""
import time


def retry_call(fn, *, attempts=3, base_delay=0.1, backoff=2.0, jitter=False,
               retryable=(Exception,)):
    for _ in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - starter bug: swallows ALL
            last = exc
    # BUG: retries with NO backoff delay and returns None on final failure
    return None
