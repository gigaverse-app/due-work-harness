"""A production capability exposed as a module-level instance, with a data policy beside it."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeedPolicy:
    """Plain configuration data: referencing it reaches no production behavior."""

    max_rearms: int = 3


@dataclass
class EventFeed:
    """A production service object whose methods are the behavior adapters bind."""

    policy: FeedPolicy = field(default_factory=FeedPolicy)
    executed: list[int] = field(default_factory=list)

    def attempt_execution(self, pk: int) -> int:
        self.executed.append(pk)
        return pk


event_feed = EventFeed()
