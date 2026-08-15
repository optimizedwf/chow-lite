# Expected Behavior — bugfix-small-011

## Corrected Implementation

```python
import re

def verify_claims(claims, evidence_text):
    out = []
    for c in claims:
        negated = c.startswith("NOT ")
        needle = c[4:] if negated else c
        m = re.search(re.escape(needle), evidence_text, re.IGNORECASE)
        if negated:
            if m:
                out.append({"claim": c, "status": "FAILED", "evidence": m.group(0)})
            else:
                out.append({"claim": c, "status": "VERIFIED", "evidence": ""})
        else:
            if m:
                out.append({"claim": c, "status": "VERIFIED", "evidence": m.group(0)})
            else:
                out.append({"claim": c, "status": "UNVERIFIED", "evidence": ""})
    return out
```

## Contract Notes

- VERIFIED is earned: the claim must actually appear in the evidence
  (case-insensitive substring). `evidence` carries the EXACT matched excerpt
  (the regex match group), never the whole corpus — a verifier that stamps
  everything VERIFIED with the full text as "evidence" is lying.
- UNVERIFIED is honest: no match, empty evidence, claim still reported.
- Negative claims (`"NOT X"`) are the audit's teeth: when X is present the
  claim is FAILED (the report contradicts the claim); when X is absent the
  claim is VERIFIED. A negative claim is NEVER dropped and never downgraded
  to UNVERIFIED when contradicted.
- Every input claim produces exactly one output verdict, order preserved.
  This mirrors nine's verify-lane doctrine (slice 41): "the audit SHIPs when
  honest — an honest UNVERIFIED still SHIPs; a cop that hides a FAIL BLOCKs."
- Standard library only (`re`); signature unchanged.
