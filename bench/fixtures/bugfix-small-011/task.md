# Eval Fixture: bugfix-small-011

## Task Description

Fix the bug in the following Python module.

The module implements `verify_claims(claims, evidence_text)`, the honest
claim-verification helper an evidence-gated agent OS uses for its audit lane
(every verdict must be earned by evidence — a verifier that lies, drops a
claim, or invents evidence is the cardinal sin; an honest UNVERIFIED still
SHIPs):

- `claims` is a list of strings; `evidence_text` is the audited document.
- Returns a list of verdict dicts, ONE per claim, in the SAME order:
  `{"claim": <original claim text>, "status": <one of "VERIFIED" | "UNVERIFIED"
  | "FAILED">, "evidence": <matched excerpt or "">}`.
- A claim whose text appears in `evidence_text` (case-insensitive substring
  match) is `VERIFIED`, and `evidence` is the EXACT matched excerpt from the
  evidence text (never the whole corpus).
- A claim that does NOT appear is `UNVERIFIED` with `evidence` = `""`
  (honest: no evidence found, never invent any).
- A claim prefixed with `"NOT "` asserts the REST of the claim is ABSENT:
  - if the rest IS found in the evidence -> `FAILED` with the matched excerpt
    (the audit caught a false negative claim);
  - if the rest is absent -> `VERIFIED` with `""`.
- Every claim MUST appear in the output — never drop a claim, never reorder.
- `status` must be exactly one of the three strings above.

**Buggy code:**
```python
import re

def verify_claims(claims, evidence_text):
    out = []
    for c in claims:
        if c.startswith("NOT "):
            needle = c[4:]
            if re.search(re.escape(needle), evidence_text, re.IGNORECASE):
                out.append({"claim": c, "status": "UNVERIFIED", "evidence": ""})
            else:
                continue  # BUG: drops the claim entirely
        elif re.search(re.escape(c), evidence_text, re.IGNORECASE):
            out.append({"claim": c, "status": "VERIFIED", "evidence": evidence_text})
        else:
            out.append({"claim": c, "status": "VERIFIED", "evidence": ""})
    return out
```

**Example expected behavior:**
```python
verify_claims(["server is down"], "The server is down since 3pm.")
# [{"claim": "server is down", "status": "VERIFIED", "evidence": "server is down"}]

verify_claims(["no monkeys"], "The server is down.")
# [{"claim": "no monkeys", "status": "UNVERIFIED", "evidence": ""}]

verify_claims(["NOT alpha"], "alpha is everywhere")
# [{"claim": "NOT alpha", "status": "FAILED", "evidence": "alpha"}]

verify_claims(["NOT zzz"], "alpha is everywhere")
# [{"claim": "NOT zzz", "status": "VERIFIED", "evidence": ""}]
```
