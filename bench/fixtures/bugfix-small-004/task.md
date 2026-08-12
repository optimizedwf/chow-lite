# Eval Fixture: bugfix-small-004

## Task Description

Fix the bug in the following Python function.

The function `merge_unique(left, right)` should merge two lists, preserving the **first-seen order** of items and removing duplicates across both lists. Items are hashable (they can be added to a set).

**Buggy code:**
```python
def merge_unique(left, right):
    return sorted(set(left + right))
```

**Example expected behavior:**
```python
>>> merge_unique([1, 2, 3], [2, 3, 4])
[1, 2, 3, 4]          # order preserved, duplicates removed

>>> merge_unique(["hello", "world"], ["world", "python"])
["hello", "world", "python"]  # order preserved, "world" not duplicated

>>> merge_unique([1, "a", (1,2)], ["a", 1, (3,)])
[1, "a", (1,2), (3,)]  # mixed hashable types, order preserved
```

## Problem Analysis

The current implementation has **two bugs**:

1. **Incorrect ordering:** `sorted(set(left + right))` returns items sorted by value rather than preserving the first-seen order from left then right.

2. **Failure on mixed hashable types:** `sorted()` in Python 3 requires consistent ordering between comparable types. Passing a list containing, for example, both integers and strings (`[1, "a"]`) will raise a `TypeError` because `int` and `str` cannot be ordered relative to each other.

## Success Criteria

- [ ] The fix correctly merges two lists preserving first-seen order (left items first, then right items not already seen)
- [ ] The fix removes duplicates across both lists
- [ ] The fix handles mixed hashable types (e.g., `int`, `str`, `tuple`) without error
- [ ] The fix handles empty input lists gracefully
- [ ] The fix does not introduce new bugs or side effects
- [ ] The function signature remains unchanged: `def merge_unique(left, right)`

## Implementation Reference

- Branch: N/A (local fixture)
- PR: N/A

## Evaluation Dimensions

- build: false
- test: true
- style: true
- security: false

## Notes

This is a minimal self-contained bugfix. The agent only needs to edit the function body — the signature should remain unchanged.
