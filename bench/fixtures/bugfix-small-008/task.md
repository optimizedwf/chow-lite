# Eval Fixture: bugfix-small-008

## Task Description

Fix the bug in the following Python module.

The module implements an agent tool that requires an environment variable
`NINE_TEST_TOKEN`. It must follow the **fail-loud** doctrine:

- `check_token(value)` — return `"ok"` only for a non-empty, non-whitespace
  token; return `"missing"` for `None`, `""`, `"   "`, or `"\t"`.
- `main()` — read `NINE_TEST_TOKEN` from the environment WITHOUT crashing
  when it is unset (no traceback); when it is missing/empty/whitespace, print
  exactly one clean line `[error] NINE_TEST_TOKEN missing or whitespace` to
  **stderr** and exit **1**; when valid, print `[ok] token accepted: <first 4
  chars>...` to stdout and exit **0**.

**Buggy code:**
```python
import os
import sys

def check_token(value):
    # BUG: whitespace is truthy, so it is treated as a valid token
    return "ok" if value else "missing"

def main():
    # BUG: unset env -> KeyError traceback instead of a clean error
    token = os.environ["NINE_TEST_TOKEN"]
    if not token:
        sys.stderr.write("[error] NINE_TEST_TOKEN is empty\n")
        return 1
    sys.stdout.write(f"[ok] token accepted: {token[:4]}...\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

**Example expected behavior (shell):**
```bash
$ env -u NINE_TEST_TOKEN python3 solution.py; echo $?
[error] NINE_TEST_TOKEN missing or whitespace
1
$ NINE_TEST_TOKEN="   " python3 solution.py; echo $?
[error] NINE_TEST_TOKEN missing or whitespace
1
$ NINE_TEST_TOKEN="sk-test-123" python3 solution.py; echo $?
[ok] token accepted: sk-t...
0
```
