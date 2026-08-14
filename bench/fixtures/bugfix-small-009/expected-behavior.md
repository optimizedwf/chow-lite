# Expected Behavior — bugfix-small-009

## Corrected Implementation

```python
import random
import time

def retry_call(fn, *, attempts=3, base_delay=0.1, backoff=2.0, jitter=False,
               retryable=(Exception,)):
    if not isinstance(attempts, int) or attempts < 1:
        raise ValueError("attempts must be an int >= 1")
    if base_delay < 0 or backoff <= 0:
        raise ValueError("base_delay >= 0 and backoff > 0 required")

    def _is_retryable(exc):
        if callable(retryable):
            return bool(retryable(exc))
        return isinstance(exc, retryable)

    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - contract: raise last after retries
            if not _is_retryable(exc):
                raise
            last = exc
            if attempt == attempts:
                break
            delay = base_delay * (backoff ** (attempt - 1))
            if jitter:
                delay += random.uniform(0.0, delay)
            time.sleep(delay)
    raise last  # model-or-fail: NEVER return None on failure
```

## Contract Notes

- Exponential backoff: retry 2 waits `base_delay`, retry 3 waits
  `base_delay * backoff`, retry n waits `base_delay * backoff ** (n - 2)`
  (zero-indexed as `base_delay * backoff ** (attempt - 1)` where attempt is
  the retry's 1-based position).
- The last exception is ALWAYS re-raised after `attempts` exhausted retries —
  returning `None` would look like a successful (empty) run and hide the
  failure (model-or-fail honesty, mirrors nine's `NodeTimeoutError` retryable
  vs `WorkflowError` deterministic classification).
- Validation mirrors nine's `Node.__post_init__` guard (T8-F4): bad `attempts`
  / negative delays fail loud up front instead of misbehaving mid-flight.
