# Expected Behavior — bugfix-small-002

## Corrected Implementation

```python
def normalize_email(email):
    # Trim whitespace
    email = email.strip()
    # Validate exactly one '@'
    if not email or email.count('@') != 1:
        raise ValueError("email must contain exactly one '@'")
    local, domain = email.split('@')
    # Preserve local part case, lowercase domain
    return f"{local}@{domain.lower()}"
```

**Or a slightly more explicit variant:**
```python
def normalize_email(email):
    if not email or not email.strip():
        raise ValueError("email must not be empty")
    email = email.strip()
    if email.count('@') != 1:
        raise ValueError("email must contain exactly one '@'")
    local, domain = email.split('@')
    return local + '@' + domain.lower()
```

## Correct Behavior Examples

| Input | Expected Output |
|-------|----------------|
| `normalize_email("  User@Example.COM  ")` | `"User@example.com"` |
| `normalize_email("USER@EXAMPLE.COM")` | `"USER@example.com"` |
| `normalize_email("John.Doe@Example.com")` | `"John.Doe@example.com"` |
| `normalize_email("user@example.com")` | `"user@example.com"` |
| `normalize_email("user@Sub.Example.COM")` | `"user@sub.example.com"` |

## Invalid Inputs

| Input | Expected Behavior |
|-------|------------------|
| `normalize_email("")` | `ValueError` |
| `normalize_email("   ")` | `ValueError` (blank after trim) |
| `normalize_email("missing-at")` | `ValueError` |
| `normalize_email("a@b@c.com")` | `ValueError` (multiple @) |

## Edge Cases to Handle

1. Leading/trailing whitespace — must be stripped before validation
2. Empty string — must raise `ValueError`
3. Blank string (whitespace only) — must raise `ValueError`
4. Missing `@` sign — must raise `ValueError`
5. Multiple `@` signs — must raise `ValueError`
6. Domain with subdomains — all levels lowercased
7. Local part with special characters (dots, etc.) — preserved as-is
