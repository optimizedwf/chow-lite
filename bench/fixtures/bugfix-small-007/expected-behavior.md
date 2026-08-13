# Expected Behavior — bugfix-small-007

## Corrected Implementation

```python
def title_case(text):
    return text[:1].upper() + text[1:].lower()
```

`text[:1]` is `""` for the empty string (never IndexError), so the empty /
whitespace / newline inputs round-trip unchanged and unicode first characters
are uppercased safely. The signature stays `title_case(text: str) -> str`.
