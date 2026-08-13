# Eval Fixture: bugfix-small-007

## Task Description

Fix the bug in the following Python function.

The function `title_case(text)` should return the text with its first
character uppercased and the rest lowercased. It must NEVER raise, even on
empty strings, whitespace-only strings, newlines, or non-ASCII unicode
(accents, CJK, symbols). Use only standard Python.

**Buggy code:**
```python
def title_case(text):
    return text[0].upper() + text[1:].lower()
```

**Example expected behavior:**
```python
title_case("hello world")
'Hello world'
title_case("")
''                       # must NOT raise IndexError
title_case("   ")
'   '
title_case("héllo")
'Héllo'                  # unicode safe
title_case("テスト")
'テスト'
```
