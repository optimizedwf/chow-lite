# Eval Fixture: bugfix-small-002

## Task Description

Fix the bug in the following Python function.

The function `normalize_email(email)` should trim leading/trailing whitespace and
lowercase only the domain part of the email (after `@`), preserving the case of
the local part (before `@`). It must also validate that exactly one `@` is present.

**Buggy code:**
```python
def normalize_email(email):
    return email.strip().lower()
```

**Example expected behavior:**
```python
>>> normalize_email("  User@Example.COM  ")
'User@example.com'   # local part "User" preserved, domain lowercased, whitespace trimmed
```

## Problem Analysis

The current implementation has **two bugs**:

1. **Over-lowercasing:** It calls `.lower()` on the entire email, which
   incorrectly lowercases the local part. The local part (before `@`) is
   case-sensitive per the email spec and should be preserved as-is.

2. **Missing validation:** It assumes the input always contains exactly one
   `@`. It should raise a `ValueError` when no `@` is present, when multiple
   `@` signs are present, or when the input is empty/blank.

## Success Criteria

- [ ] The fix correctly preserves the case of the local part (before `@`)
- [ ] The fix lowercases only the domain part (after `@`)
- [ ] The fix trims leading/trailing whitespace from the entire input
- [ ] The fix raises `ValueError` for missing `@`
- [ ] The fix raises `ValueError` for multiple `@` signs
- [ ] The fix raises `ValueError` for empty or blank input (after trimming)
- [ ] The fix does not introduce new bugs or side effects
- [ ] The function signature remains unchanged: `def normalize_email(email)`

## Implementation Reference

- Branch: N/A (local fixture)
- PR: N/A

## Evaluation Dimensions

- build: false
- test: true
- style: true
- security: false

## Notes

This is a minimal self-contained bugfix. The agent only needs to edit the
function body — the signature should remain unchanged.
