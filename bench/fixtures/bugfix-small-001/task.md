# Eval Fixture: bugfix-small-001

## Task Description

Fix the off-by-one error in the following Python function.

The function `slice_list(items, start, end)` should return a new list containing
items from index `start` to index `end` **inclusive**. The current implementation
uses Python's slice notation which is exclusive of the end index.

**Buggy code:**
```python
def slice_list(items, start, end):
    return items[start:end]
```

**Example expected behavior:**
```python
>>> slice_list([10, 20, 30, 40, 50], 1, 3)
[20, 30, 40]      # indices 1, 2, and 3 inclusive — NOT [20, 30]
```

## Success Criteria

- [ ] The fix correctly returns items from `start` to `end` inclusive
- [ ] The fix handles `end >= len(items)` gracefully (returns up to the end of the list)
- [ ] The fix handles `start < 0` gracefully (treats as 0)
- [ ] The fix handles empty list input without error
- [ ] The fix does not introduce new bugs or side effects
- [ ] The function signature remains unchanged

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
function body — the signature and docstring (if any) should remain unchanged.
