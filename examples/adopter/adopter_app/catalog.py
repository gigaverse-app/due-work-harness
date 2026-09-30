"""A small durable catalog publisher; queues are wakeups, SQLite owns intent.

A product can change while a timed-out remote write is still running. Recovery
compares the remote value as well as the acknowledged revision, because a late
old write can undo an otherwise successful newer write. Product approval gates
execution without creating another revision. Receipt ingestion merges facts
rather than treating the first receipt as the complete response.
"""

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path


class Catalog:
    """Application commands and workers over one connection, with injected external ports."""

    def __init__(
        self,
        path: str | Path,
        *,
        write: Callable[[int, str], None],
        read: Callable[[int], str],
        notify: Callable[[int], None],
        receipts: Callable[[], Iterable[tuple[int, str]]],
        request_receipts: Callable[[], None] = lambda: None,
        connection_factory: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> None:
        self.path = path
        self.request_provider_receipts = request_receipts
        self.db = sqlite3.connect(path, check_same_thread=False, factory=connection_factory)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY, desired TEXT NOT NULL, revision INTEGER NOT NULL,
                approved INTEGER NOT NULL, acknowledged INTEGER
            );
            CREATE TABLE IF NOT EXISTS obligations (id INTEGER PRIMARY KEY REFERENCES products(id));
            CREATE TABLE IF NOT EXISTS receipt_attempts (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS receipts (attempt INTEGER, name TEXT, PRIMARY KEY(attempt, name));
        """)
        self.write, self.read, self.notify, self.receipts = write, read, notify, receipts

    def admit(self, value: str, *, approved: bool = True) -> int:
        """Own admission's transaction; wakeups may be lost after commit."""
        with self.db:
            row = self.db.execute(
                "INSERT INTO products(desired, revision, approved) VALUES (?, 1, ?) RETURNING id", (value, approved)
            ).fetchone()
            assert row is not None
            identity = int(row[0])
            self.db.execute("INSERT INTO obligations VALUES (?)", (identity,))
        self.notify(identity)
        return identity

    def change(self, identity: int, value: str) -> None:
        """A new command always gets a new revision, including A -> B -> A."""
        with self.db:
            self.db.execute("UPDATE products SET desired=?, revision=revision+1 WHERE id=?", (value, identity))
        self.notify(identity)

    def retire(self, identity: int) -> None:
        """Retain a tombstone so reconciliation can undo a late accepted publication."""
        self.change(identity, "")

    def approve(self, identity: int) -> None:
        """Release existing intent without changing its identity, revision or retry state."""
        with self.db:
            self.db.execute("UPDATE products SET approved=1 WHERE id=?", (identity,))
        self.notify(identity)

    def reserved(self, identity: int) -> tuple[int, str, int]:
        row = self.db.execute("SELECT id, desired, revision FROM products WHERE id=?", (identity,)).fetchone()
        assert row is not None
        return row

    def acknowledged(self, identity: int) -> int | None:
        return self.db.execute("SELECT acknowledged FROM products WHERE id=?", (identity,)).fetchone()[0]

    def owed(self) -> list[int]:
        """Durable recovery owners survive acknowledgements and late remote writes."""
        return [row[0] for row in self.db.execute("SELECT id FROM obligations ORDER BY id")]

    def eligible(self) -> list[int]:
        return [row[0] for row in self.db.execute("SELECT id FROM products WHERE approved=1 ORDER BY id")]

    def execute(self, identity: int) -> None:
        if identity not in self.eligible():
            return
        _, value, revision = self.reserved(identity)
        try:
            self.write(identity, value)
        except TimeoutError:
            return  # Uncertainty retains the durable owner for reconciliation.
        with self.db:
            # A stale result cannot acknowledge a newer command.
            self.db.execute(
                "UPDATE products SET acknowledged=? WHERE id=? AND revision=?", (revision, identity, revision)
            )

    def recover(self) -> None:
        for identity in self.eligible():
            _, value, revision = self.reserved(identity)
            # An ack alone cannot detect an older accepted request completing late.
            if self.acknowledged(identity) != revision or self.read(identity) != value:
                self.execute(identity)

    def ingest_receipts(self) -> None:
        """Duplicates, batches and partial responses all accumulate the same durable facts."""
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO receipts VALUES (?, ?)", list(self.receipts()))

    def request_receipts(self, replay: Callable[[], None] = lambda: None) -> None:
        """Start a fresh sender turn without rewriting the previous turn's evidence."""
        with self.db:
            self.db.execute("INSERT INTO receipt_attempts DEFAULT VALUES")
        replay()
        self.request_provider_receipts()

    def received(self, attempt: int = 1) -> frozenset[str]:
        return frozenset(row[0] for row in self.db.execute("SELECT name FROM receipts WHERE attempt=?", (attempt,)))
