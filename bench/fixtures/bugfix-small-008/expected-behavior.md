# Expected Behavior — bugfix-small-008

## Corrected Implementation

```python
import os
import sys

def check_token(value):
    return "ok" if value and value.strip() else "missing"

def main():
    token = os.environ.get("NINE_TEST_TOKEN", "")
    if check_token(token) != "ok":
        sys.stderr.write("[error] NINE_TEST_TOKEN missing or whitespace\n")
        return 1
    sys.stdout.write(f"[ok] token accepted: {token.strip()[:4]}...\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

`os.environ.get(..., "")` never raises when the variable is unset; `.strip()`
turns whitespace-only values into `""` so they fail loud. Exactly one
`[error]` line on stderr, exit 1 — no traceback, no retry loop, no silent
default.
