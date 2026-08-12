# Expected Behavior — bugfix-small-001

## Corrected Implementation

```python
def slice_list(items, start, end):
    # Ensure start is non-negative
    start = max(0, start)
    # Ensure end is within bounds (inclusive, so we add 1 for slice end)
    end = min(end, len(items) - 1) if items else -1
    # Return the slice inclusive of end
    return items[start:end + 1] if end >= start else []
```

**Or more concisely:**
```python
def slice_list(items, start, end):
    return items[max(0, start):end + 1]
```

## Correct Behavior Examples

| Input | Expected Output |
|-------|----------------|
| `slice_list([10, 20, 30, 40, 50], 1, 3)` | `[20, 30, 40]` |
| `slice_list([10, 20, 30, 40, 50], 0, 2)` | `[10, 20, 30]` |
| `slice_list([10, 20, 30, 40, 50], 3, 4)` | `[40, 50]` |
| `slice_list([10, 20, 30, 40, 50], 0, 0)` | `[10]` |
| `slice_list([10, 20, 30, 40, 50], 1, 10)` | `[20, 30, 40, 50]` (end clamped to len-1) |
| `slice_list([], 0, 0)` | `[]` (empty input) |
| `slice_list([10, 20, 30], -1, 1)` | `[10, 20]` (start clamped to 0) |

## Edge Cases to Handle

1. `start` > `end` — should return empty list `[]`
2. `start` < 0 — should clamp to 0
3. `end` >= `len(items)` — should clamp to `len(items) - 1`
4. Empty input list — should return `[]`
5. `start` == `end` — should return single-element list
