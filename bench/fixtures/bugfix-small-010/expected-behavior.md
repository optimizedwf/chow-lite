# Expected Behavior — bugfix-small-010

## Corrected Implementation

```python
import threading

class CancellableWorker:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    def run(self, steps):
        for n, step in enumerate(steps, start=1):
            if self._event.is_set():
                return ("cancelled", n - 1)
            step()
        return ("completed", len(steps))
```

## Contract Notes

- The flag is checked BEFORE every step: a cancelled worker performs no
  further work — including steps that would crash or write. Once cancelled,
  `run` returns `("cancelled", n)` and raises nothing.
- `threading.Event.set()` is idempotent and thread-safe by construction:
  double `cancel()` never raises, and a concurrent `cancel()` during `run`
  is visible to the next pre-step check (no lock needed, no lost wakeup).
- This mirrors nine's runtime doctrine: an operator-cancelled job must not
  produce new evidence and its CANCELLED verdict must be durable
  (torture-18 F3: `_abort_cancelled` persists the verdict) — cooperative
  cancellation is the fixture-shaped form of that invariant.
- Reuse after cancellation is undefined: once cancelled, the worker stays
  cancelled (Event semantics). Tests only assert the specified surface.
