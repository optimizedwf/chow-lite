# Expected Behavior — bugfix-small-006

## Corrected Implementation

```python
import json

def _strict_loads(s):
    # json.loads accepts NaN/Infinity by default; strict mode must reject them
    def _reject(_token):
        raise ValueError("non-standard JSON constant")
    return json.loads(s, parse_constant=_reject)

def render_eval_json(checks):
    out = []
    for c in checks:
        out.append({
            "name": c.get("name", ""),
            "passed": bool(c.get("passed", False)) is True if isinstance(c.get("passed"), bool) else c.get("passed", False),
            "message": c.get("message", ""),
        })
    return json.dumps({"checks": out})

def validate_eval_json(s):
    try:
        data = _strict_loads(s)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    checks = data.get("checks")
    if not isinstance(checks, list):
        return False
    for c in checks:
        if not isinstance(c, dict):
            return False
        if "name" not in c or not isinstance(c["name"], str):
            return False
        if "passed" not in c or not isinstance(c["passed"], bool):
            return False
        if "message" not in c or not isinstance(c["message"], str):
            return False
    return True
```

The strict validator rejects every non-standard shape: stringified booleans,
`1`/`0`, `null`, missing keys, `NaN`/`Infinity` (via `parse_constant`),
trailing commas (native `json` already rejects them), and non-list `checks`.
