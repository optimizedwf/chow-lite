# Eval Fixture: bugfix-small-005

## Task Description

Fix the bug in the following Python function.

The function `count_words(text)` should return a dictionary counting **case-insensitive** words, stripping **simple punctuation at word edges**, and ignoring empty tokens. Use only standard Python.

**Buggy code:**
```python
from collections import Counter

def count_words(text):
    return dict(Counter(text.split()))
```

**Example expected behavior:**
```python
>>> count_words("Hello world! Hello")
{'hello': 2, 'world': 1}
# 'Hello' and 'hello' are counted together (case-insensitive)
# 'world!' has its trailing '!' stripped
```

## Problem Analysis

The current implementation has **two bugs**:

1. **Case sensitivity:** The implementation does not lowercase words, so `"Hello"` and `"hello"` are treated as distinct tokens instead of being counted together.

2. **No punctuation stripping:** The implementation does not strip punctuation from word edges, so `"world!"` and `"world"` are treated as distinct tokens.

## Success Criteria

- [ ] The fix lowercases all words before counting (case-insensitive matching)
- [ ] The fix strips simple punctuation (`.`, `,`, `!`, `?`, `;`, `:`, `'`, `"`, `(`, `)`, `[`, `]`, `{`, `}`) from both edges of each word
- [ ] The fix preserves internal punctuation (e.g., apostrophes in `"don't"`)
- [ ] The fix ignores empty tokens (including tokens that are entirely punctuation)
- [ ] The fix handles repeated whitespace, newlines, and tabs correctly
- [ ] The fix returns `{}` for empty string and punctuation-only input
- [ ] The fix treats numeric tokens as regular words
- [ ] The function signature remains unchanged: `def count_words(text)`
- [ ] The fix uses only standard Python (no external dependencies)

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
function body — the signature should remain unchanged. The `from collections import Counter` import can be changed or removed as needed.
