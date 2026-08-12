# Expected Behavior — bugfix-small-003

## Corrected Implementation

```python
def parse_int_list(text):
    if not text.strip():
        return []
    result = []
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            raise ValueError(f"invalid integer: {part!r}")
    return result
```

**Or a more concise variant:**

```python
def parse_int_list(text):
    trimmed = text.strip()
    if not trimmed:
        return []
    return [
        int(part)
        for part in trimmed.replace(' ', '').split(',')
        if part.strip()
    ]
```

(Note: the concise variant assumes simple whitespace trimming. The explicit
loop version is preferred for clarity and robustness.)

## Correct Behavior Examples

| Input | Expected Output |
|-------|----------------|
| `parse_int_list("1,2,3")` | `[1, 2, 3]` |
| `parse_int_list(" 1, 2 , 3 ")` | `[1, 2, 3]` |
| `parse_int_list("-1,0,42")` | `[-1, 0, 42]` |
| `parse_int_list("1,,2,3")` | `[1, 2, 3]` (empty segment skipped) |
| `parse_int_list("1,2,")` | `[1, 2]` (trailing comma skipped) |

## Edge Cases to Handle

| Input | Expected Behavior |
|-------|------------------|
| `parse_int_list("")` | `[]` |
| `parse_int_list("   ")` | `[]` (whitespace-only) |
| `parse_int_list("1,a,3")` | `ValueError` ('a' is not an integer) |
| `parse_int_list("1,2,three")` | `ValueError` ('three' is not an integer) |

## Edge Cases to Handle

1. Empty string returns `[]`
2. Whitespace-only string returns `[]` (after stripping)
3. Leading/trailing whitespace around tokens — must be stripped
4. Repeated commas produce empty segments — must be ignored
5. Trailing comma produces empty final segment — must be ignored
6. Invalid integer tokens must raise `ValueError`
7. Mixed valid and invalid tokens must still raise `ValueError` (no partial result)
8. Negative integers must be parsed correctly
