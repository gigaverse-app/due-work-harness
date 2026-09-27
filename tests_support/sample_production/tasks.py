"""Production task entry points the contract self-tests name as covered sources."""


def cleanup_task() -> None:
    """A task that publishes work after its transaction commits; its body is never run by the self-tests."""


def reindex_task() -> None:
    """A second, independent publisher."""
