"""BUGGY STARTER for bugfix-small-011.

Bugs (each maps to a check.sh case):
1. absent claims are stamped VERIFIED (the lie) and present claims get the
   WHOLE corpus as evidence instead of the exact excerpt.
2. absent NEGATIVE claims are dropped entirely (no verdict emitted).
3. a negative claim CONTRADICTED by evidence is downgraded to UNVERIFIED
   instead of FAILED.
"""
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
