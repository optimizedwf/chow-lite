from collections import Counter

def count_words(text):
    # BUG: doesn't handle case-insensitivity or punctuation stripping
    return dict(Counter(text.split()))
