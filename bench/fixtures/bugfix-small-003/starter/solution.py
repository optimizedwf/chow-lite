def parse_int_list(text):
    # BUG: doesn't handle whitespace, empty segments, or invalid tokens
    return [int(x) for x in text.split(',')]
