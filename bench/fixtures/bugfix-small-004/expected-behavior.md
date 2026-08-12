# Expected Behavior — bugfix-small-004

## Corrected Implementation

```python
def merge_unique(left, right):
    seen = set()
    result = []
    for item in left + right:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
```

**Or using `dict.fromkeys` (Python 3.7+ preserves insertion order):**

```python
def merge_unique(left, right):
    return list(dict.fromkeys(left + right))
```

## Correct Behavior Examples

| Input | Expected Output |
|-------|----------------|
| `merge_unique([1, 2, 3], [2, 3, 4])` | `[1, 2, 3, 4]` |
| `merge_unique([1, 1, 2, 3], [4, 5])` | `[1, 2, 3, 4, 5]` |
| `merge_unique([1, 2], [2, 2, 3, 3])` | `[1, 2, 3]` |
| `merge_unique([1, 2], [3, 4])` | `[1, 2, 3, 4]` |
| `merge_unique([], [1, 2, 3])` | `[1, 2, 3]` |
| `merge_unique([1, 2, 3], [])` | `[1, 2, 3]` |
| `merge_unique([], [])` | `[]` |
| `merge_unique(["hello", "world"], ["world", "python"])` | `["hello", "world", "python"]` |
| `merge_unique([1, "a", (1,2)], ["a", 1, (3,)])` | `[1, "a", (1,2), (3,)]` |

## Edge Cases to Handle

1. Duplicates within `left` list — should only keep the first occurrence
2. Duplicates within `right` list — should only keep the first occurrence
3. Overlap between `left` and `right` — items in `right` that already appeared in `left` should be skipped
4. Empty `left` list — should return unique items from `right`
5. Empty `right` list — should return unique items from `left`
6. Both empty — should return `[]`
7. Mixed hashable types (e.g., `int`, `str`, `tuple`) — must preserve order without sorting
