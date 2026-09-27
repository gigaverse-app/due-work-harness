"""A read-through cache: the production path an exemption can name as absorbing a lost dispatch."""


def rederive(entries: set[int], key: int) -> None:
    """Rebuild the entry for ``key`` on read, as a cache miss does."""
    entries.add(key)


def read_only(entries: set[int], key: int) -> bool:
    """Read the entry without rebuilding it."""
    return key in entries
