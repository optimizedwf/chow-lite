# Eval Fixture: bugfix-small-003

## Task Description

Fix the bug in the following Python function.

The function `parse_int_list(text)` should parse a comma-separated string of integers,
trim whitespace from each token, ignore empty segments caused by repeated or trailing
commas, and return a list of integers. If any token is not a valid integer, the
function must raise a `ValueError`.

**Buggy code:**
```python
def parse_int_list(text):
    return [int(x) for x in text.split(',')]
```

**Example expected behavior:**
```python
>>> parse_int_list("1, 2, 3")
[1, 2, 3]

>>> parse_int_list("1,,2,")
[1, 2]

>>> parse_int_list("")
[]

>>> parse_int_list("1,a,3")
ValueError: invalid integer: 'a'
```

## Problem Analysis

The current implementation has **three bugs**:

1. **No whitespace handling:** The string is split directly on commas without
   stripping whitespace from each token, so `"1, 2, 3"` would try `int(" 2")`
   and `int(" 3")` (which Python actually handles, but this is incidental —
   the spec requires explicit trimming for correctness and clarity).

2. **No empty-segment handling:** Repeated commas (`"1,,2"`) or trailing commas
   (`"1,2,"`) produce empty strings after split, which `int("")` rejects with
   a `ValueError` instead of silently ignoring them.

3. **No validation of non-integer tokens:** The split-then-cast approach does
   raise `ValueError` for invalid tokens like `"a"`, but it doesn't provide a
   meaningful error message. More importantly, the bare list comprehension
   conflates empty/invalid errors, making it impossible to distinguish between
   an empty string (which should be ignored) and a truly invalid token (which
   should raise `ValueError`).

## Success Criteria

- [ ] The fix correctly parses comma-separated integers
- [ ] The fix trims whitespace from each token
- [ ] The fix ignores empty segments from repeated commas
- [ ] The fix ignores empty segments from trailing commas
- [ ] The fix returns `[]` for empty string input
- [ ] The fix returns `[]` for whitespace-only input
- [ ] The fix raises `ValueError` for invalid integer tokens
- [ ] The fix raises `ValueError` when there are both valid and invalid tokens
- [ ] The fix does not introduce new bugs or side effects
- [ ] The function signature remains unchanged: `def parse_int_list(text)`

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
