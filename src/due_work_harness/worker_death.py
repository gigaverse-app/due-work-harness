class WorkerDied(BaseException):
    """A worker died: nothing it would have done next happens. A BaseException, so ordinary error handling cannot swallow it."""


class CallbackFailed(Exception):  # noqa: N818 - named for what happened, like WorkerDied
    """
    An after-commit callback failed, as a real one does: a bug, a timeout, a provider error.

    An ordinary ``Exception``, unlike :class:`WorkerDied`: the process lives on,
    so what happens next is the framework's and the application's own error
    handling. Django, for one, skips every later non-robust callback of the same
    commit and re-raises; a ``robust=True`` callback's failure is logged instead.
    """
