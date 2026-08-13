import json

def render_eval_json(checks):
    # BUG: stringifies booleans and hand-formats JSON -> "passed": "true" strings
    parts = []
    for c in checks:
        parts.append('    {"name": "%s", "passed": "%s", "message": "%s"}' % (
            c.get("name", ""), str(c.get("passed", False)).lower(), c.get("message", "")))
    return '{\n  "checks": [\n' + ",\n".join(parts) + '\n  ]\n}'

def validate_eval_json(s):
    # BUG: lenient - accepts "true"/"false" strings and 1/0 as passed
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
