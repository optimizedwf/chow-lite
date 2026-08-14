# Eval Fixture: bugfix-small-009

## Task Description

Fix the bug in the following Python module.

The module implements `retry_call`, the retry-with-exponential-backoff helper
an evidence-gated agent OS uses for transient model/API failures:

- `retry_call(fn, *, attempts=3, base_delay=0.1, backoff=2.0, jitter=False, retryable=(Exception,))`
  calls `fn()` up to `attempts` times and returns the first successful result.
- Only exceptions that pass `retryable` are retried. `retryable` may be an
  exception type/tuple (e.g. `(IOError,)`) or a predicate callable
  `(exc) -> bool` (e.g. retry only on HTTP 429/503).
- A non-retryable exception is re-raised IMMEDIATELY (no retries).
- Before retry *n* (n = 2..attempts) the helper waits `base_delay * backoff ** (n-1)`
  seconds (exponential backoff); `jitter=True` adds a random delay in
  `[0, delay)` on top (never negative).
- After the LAST attempt the helper MUST re-raise the last exception — it
  must NEVER return `None` on failure (a silent success would ship broken
  work: model-or-fail honesty).
- `attempts` must be an int >= 1 (`ValueError` otherwise); `base_delay >= 0`
  and `backoff > 0` (`ValueError` otherwise).

**Buggy code:**
```python
import time

def retry_call(fn, *, attempts=3, base_delay=0.1, backoff=2.0, jitter=False,
               retryable=(Exception,)):
    for _ in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
    # BUG: retries with NO backoff delay (hammering) and returns None on
    # final failure instead of raising the last exception
    return None
```

**Example expected behavior:**
```python
calls = []
def flaky():
    calls.append(1)
    if len(calls) < 3:
        raise IOError("transient")
    return "ok"

retry_call(flaky, attempts=3, base_delay=0.0) == "ok"   # True
len(calls) == 3                                          # True
retry_call(lambda: 1 / 0, attempts=2, base_delay=0.0)    # raises ZeroDivisionError
retry_call(lambda: 1, attempts=0)                        # raises ValueError
```
