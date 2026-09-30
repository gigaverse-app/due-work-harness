"""An actual persisted handoff for the MongoDB host's fault-detection controls."""

from typing import Any


class MongoDBOutbox:
    def __init__(self, collection: Any, send: Any) -> None:
        self.collection = collection
        self.send = send

    def place(self, identity: str) -> None:
        self.collection.insert_one({"_id": identity, "done": False})
        self.recover()

    def recover(self) -> None:
        for row in self.collection.find({"done": False}):
            self.send(row["_id"])
            self.collection.update_one({"_id": row["_id"]}, {"$set": {"done": True}})
