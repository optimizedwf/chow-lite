# Eval Fixture: bugfix-small-006

## Task Description

Fix the bug in the following Python module.

The module implements two functions used by an evidence-gated agent:

- `render_eval_json(checks)` — given a list of check dicts
  (`{"name": str, "passed": bool, "message": str}`), return a **STRICT** JSON
  string: `json.loads` must succeed, every check must keep `name` (str),
  `passed` (exactly a JSON boolean `true`/`false`), and `message` (str). The
  output must never contain stringified booleans, numbers for booleans,
  `NaN`, trailing commas, or missing keys.
- `validate_eval_json(s)` — return `True` ONLY when `s` is strict JSON with
  the exact shape above AND every `passed` value is a literal JSON boolean
  (`true`/`false`). It must REJECT `"true"`/`"false"` strings, `1`/`0`,
  `null`, missing `passed`, `NaN`, trailing commas, and non-list `checks`.

**Buggy code:**
```python
import json

def render_eval_json(checks):
    parts = []
    for c in checks:
        parts.append('    {"name": "%s", "passed": "%s", "message": "%s"}' % (
            c.get("name", ""), str(c.get("passed", False)).lower(), c.get("message", "")))
    return '{\n  "checks": [\n' + ",\n".join(parts) + '\n  ]\n}'

def validate_eval_json(s):
    try:
        data = json.loads(s)
    except Exception:
        return False
    checks = data.get("checks", [])
    if not isinstance(checks, list):
        return False
    for c in checks:
        if c.get("passed") not in (True, False, "true", "false", 1, 0):
            return False
    return True
```

**Example expected behavior:**
```python
validate_eval_json(render_eval_json([{"name": "tests", "passed": True}]))
True
validate_eval_json('{"checks":[{"name":"t","passed":"true"}]}')
False   # stringified boolean must be rejected
validate_eval_json('{"checks":[{"name":"t","passed":1}]}')
False   # int 1 must be rejected
```
