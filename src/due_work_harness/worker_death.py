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


class ReceiverFailed(Exception):  # noqa: N818 - named for what happened, like WorkerDied
    """
    A signal receiver failed, as a real one does: a bug, or the backend it reports to unreachable.

    An ordinary ``Exception``: the process lives on. What happens next is the
    framework's own dispatch and the sender's error handling. Django's
    ``Signal.send`` propagates it to the sender and skips the receivers after
    it; ``send_robust`` logs it and carries on.
    """


class CommitWorker:
    """Count a client's durable writes and fence every later operation after death."""

    def __init__(self, kill_after: int | None) -> None:
        self._kill_after = kill_after
        self.commits = 0
        self.dead = False

    def committed(self) -> None:
        self.commits += 1
        if self.commits == self._kill_after:
            self.kill_now(f"worker died right after commit {self.commits}")

    def kill_now(self, reason: str) -> None:
        self.dead = True
        raise WorkerDied(reason)

    def refuse_if_dead(self) -> None:
        if self.dead:
            raise WorkerDied("the worker is dead; its connection sends nothing more")
