# Expected Behavior — bugfix-small-005

## Corrected Implementation

```python
import string

def count_words(text):
    words = text.split()
    counts = {}
    for word in words:
        word = word.strip(string.punctuation)
        if not word:
            continue
        word = word.lower()
        counts[word] = counts.get(word, 0) + 1
    return counts
```

**Or using `collections.Counter` (equally valid):**

```python
from collections import Counter
import string

def count_words(text):
    words = text.split()
    cleaned = [word.strip(string.punctuation).lower()
               for word in words if word.strip(string.punctuation)]
    return dict(Counter(cleaned))
```

## Correct Behavior Examples

| Input | Expected Output |
|-------|----------------|
| `count_words("hello world foo")` | `{'hello': 1, 'world': 1, 'foo': 1}` |
| `count_words("Hello hello HELLO")` | `{'hello': 3}` |
| `count_words("hello! world?")` | `{'hello': 1, 'world': 1}` |
| `count_words("hello!!! ...world...")` | `{'hello': 1, 'world': 1}` |
| `count_words("hello   world\n\n\nfoo")` | `{'hello': 1, 'world': 1, 'foo': 1}` |
| `count_words("")` | `{}` |
| `count_words("!!! ??? ...")` | `{}` |
| `count_words("123 456 123")` | `{'123': 2, '456': 1}` |
| `count_words("don't can't don't")` | `{"don't": 2, "can't": 1}` |

## Edge Cases to Handle

1. **Case-insensitive duplicates** — `"Hello hello HELLO"` should count as 3 occurrences of `"hello"`
2. **Trailing punctuation** — `"hello!"` → `"hello"`, `"world?"` → `"world"`
3. **Leading punctuation** — `"!hello"` → `"hello"`, `"...world"` → `"world"`
4. **Punctuation-only tokens** — `"!!! ??? ..."` should produce no tokens, returning `{}`
5. **Repeated whitespace** — multiple spaces, newlines, tabs should all be treated as word separators
6. **Empty string** — returns `{}`
7. **Numeric tokens** — `"123"` is a valid word, counted like any other token
8. **Internal apostrophes** — `"don't"` should preserve the apostrophe, not split into `"don"` and `"t"`
9. **Mixed punctuation** — `"hello!!!"` → `"hello"`, `"...world..."` → `"world"`
