def title_case(text):
    # BUG: crashes with IndexError on the empty string
    return text[0].upper() + text[1:].lower()
