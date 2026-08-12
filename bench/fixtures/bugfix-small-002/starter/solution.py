def normalize_email(email):
    # BUG: lowercases entire email and doesn't validate exactly one '@'
    return email.strip().lower()
