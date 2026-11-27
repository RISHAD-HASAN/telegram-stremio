"""Bounded in-memory collections to prevent unbounded growth.

Provides a dict-like container that evicts the oldest entry when full,
useful for caches and in-flight request trackers where memory must stay
predictable under load.
"""


class BoundedDict(dict):
    """A dict that evicts the oldest (first-inserted) key when it exceeds maxlen."""

    def __init__(self, maxlen: int):
        super().__init__()
        self.maxlen = maxlen

    def __setitem__(self, key, value):
        if key not in self and len(self) >= self.maxlen:
            # Evict the oldest (first-inserted) entry to make room.
            self.pop(next(iter(self)))
        super().__setitem__(key, value)
