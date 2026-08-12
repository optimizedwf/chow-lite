def slice_list(items, start, end):
    # BUG: off-by-one — should be items[start:end+1] for inclusive end
    return items[start:end]
