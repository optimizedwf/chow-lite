"""BUGGY STARTER for bugfix-small-010.

Bugs (each maps to a check.sh case):
1. run() IGNORES the flag -> a cancelled worker always completes every step
   (work runs after cancel: the CANCELLED-verdict lie).
2. cancel() is NOT idempotent -> the second call raises RuntimeError.
3. cancel() before run() still executes steps (no pre-step flag check), so
   no_work_after_cancel and mid-run partial counts are wrong.
"""
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
