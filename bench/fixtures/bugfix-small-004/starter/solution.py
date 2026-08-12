def merge_unique(left, right):
    # BUG: sorted(set(...)) changes order and fails on mixed hashable types
    return sorted(set(left + right))
