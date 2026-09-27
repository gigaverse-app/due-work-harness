"""A production capability exposed as a module-level instance, with a data policy beside it."""

from pydantic import BaseModel, ConfigDict, Field


class FeedPolicy(BaseModel):
    """Plain configuration data: referencing it reaches no production behavior."""

    model_config = ConfigDict(frozen=True)

    max_rearms: int = 3


class EventFeed(BaseModel):
    """A production service object whose methods are the behavior adapters bind."""

    policy: FeedPolicy = Field(default_factory=FeedPolicy)
    executed: list[int] = Field(default_factory=list)

    def attempt_execution(self, pk: int) -> int:
        self.executed.append(pk)
        return pk


event_feed = EventFeed()
