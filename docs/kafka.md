# Kafka consumer offset failures

Install `due-work-harness[kafka]` for aiokafka. Pass a real `AIOKafkaConsumer` with
`enable_auto_commit=False` to `kafka_worker_killer(consumer)` or
`kafka_offset_reply_breaker(consumer)`. Both return context-manager factories
accepting a one-based offset-commit number, or `None` to count only.

The consumer sends its real commit to the broker. After acknowledgement the
first adapter raises `WorkerDied` and fences later `getone`, `getmany` and `commit`
calls on that consumer. The second raises aiokafka's `RequestTimedOutError` while
the worker remains alive. Restart a consumer with the same group and inspect the
broker's committed offsets/replayed records; do not reconstruct replay in a fake.

Use these capabilities in a `Host` for offset-only histories, or directly around
an application's consumer. A MongoDB history can separately interrupt its durable
writes before the application reaches its offset commit. Use `kafka_fenced(consumer, database_worker)` around that transition so
shutdown cannot commit offsets after the database worker dies. Do not replace the
application's handler, completion-watermark or retry logic with the adapter.

Scope is explicit: consumer offset acknowledgements, not Kafka transactions,
producer deliveries, rebalances or a whole-process kill. Auto-commit is refused
because its background commits bypass the observed method. Concurrent background
operations already in flight require process-level testing. The adapter does not
fence unrelated clients or external side effects.

The broker tests compare uncommitted, normally committed, worker-death and
lost-reply histories. A restarted consumer repeats the first record only in the
uncommitted control. They run against a real single-node broker on localhost.
