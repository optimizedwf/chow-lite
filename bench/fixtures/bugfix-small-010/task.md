# Eval Fixture: bugfix-small-010

## Task Description

Fix the bug in the following Python module.

The module implements `CancellableWorker`, the cooperative-cancellation
primitive an evidence-gated agent OS uses to stop a long job cleanly (the
runtime's CANCELLED verdict must be durable and no work may run after the
operator cancels):

- `cancel()` sets the cancellation flag. It is IDEMPOTENT — calling it twice
  (or more) is always safe and never raises.
- `run(steps)` executes the steps in order, checking the flag BEFORE each
  step:
  - if cancelled mid-way it returns `("cancelled", n)` where `n` is the
    number of steps completed, performs NO further work, and raises nothing;
  - `cancel()` called BEFORE `run()` makes `run` return `("cancelled", 0)`
    immediately — no step ever starts;
  - when all steps finish without cancellation it returns
    `("completed", len(steps))`.
- The worker must be THREAD-SAFE: a concurrent `cancel()` during a running
  `run()` (from another thread) must never let a step start after the flag
  is set, and must never corrupt state. Use `threading.Event`.

**Buggy code:**
```python
import threading

class CancellableWorker:
    def __init__(self):
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            if self._cancelled:
                raise RuntimeError("already cancelled")
            self._cancelled = True

    def run(self, steps):
        for step in steps:
            step()  # BUG: ignores the flag - always completes every step
        return ("completed", len(steps))
```

**Example expected behavior:**
```python
w = CancellableWorker()
w.run([lambda: None, lambda: None])            # ("completed", 2)
w.cancel()
w.run([lambda: None])                          # ("cancelled", 0)
w.cancel()                                     # safe (idempotent), no raise

w2 = CancellableWorker()
w2.run([lambda: None, lambda: w2.cancel(), lambda: 1 / 0])  # ("cancelled", 2)
# the crashing third step never runs because the flag is checked first
```
