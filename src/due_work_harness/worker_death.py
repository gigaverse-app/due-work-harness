class WorkerDied(BaseException):
    """A worker died: nothing it would have done next happens. A BaseException, so ordinary error handling cannot swallow it."""
