# aiokafka consumer offset failures

This adapter is specific to the Python **aiokafka** client. It instruments
`AIOKafkaConsumer` methods and raises aiokafka exceptions; it is not a
client-independent Kafka adapter. Clients such as `confluent-kafka` require their
own adapter, even though the broker-level recovery principles are the same.

Install `pytest-obligation[aiokafka]` and import helpers from
`due_work_harness.integrations.aiokafka`. Pass a real `AIOKafkaConsumer` with
`enable_auto_commit=False` to `aiokafka_worker_killer(consumer)` or
`aiokafka_offset_reply_breaker(consumer)`. Both return context-manager factories
accepting a one-based offset-commit number, or `None` to count only.

The consumer sends its real commit to the broker. After acknowledgement the
first adapter raises `WorkerDied` and fences later `getone`, `getmany` and `commit`
calls on that consumer. A fetch already waiting also refuses to deliver its result
after the worker dies. Empty offset commits do not count as durable writes. The second raises aiokafka's `RequestTimedOutError` while
the worker remains alive. Restart a consumer with the same group and inspect the
broker's committed offsets/replayed records; do not reconstruct replay in a fake.

Use these capabilities in a `Host` for offset-only histories, or directly around
an application's consumer. A MongoDB history can separately interrupt its durable
writes before the application reaches its offset commit. Use `aiokafka_fenced(consumer, database_worker)` around that transition so
shutdown cannot commit offsets after the database worker dies. Do not replace the
application's handler, completion-watermark or retry logic with the adapter.

Scope is explicit: consumer offset acknowledgements, not Kafka transactions,
producer deliveries, rebalances or a whole-process kill. Auto-commit is refused
because its background commits bypass the observed method. Concurrent background
operations already in flight require process-level testing. The adapter does not
fence unrelated clients or external side effects.

The broker tests compare uncommitted, normally committed, worker-death and
lost-reply histories. A restarted consumer repeats the first record only in the
uncommitted and database-death controls. They run against a real single-node broker on localhost.

A commit already in flight can still land at the broker after worker death. The
adapter cannot roll it back, but it checks the fence again before returning the
acknowledgement to application code. The broker suite holds the real coordinator
commit lock to prove both the live-worker and dead-worker outcomes.

## Required adoption surface

Bind these aiokafka fault boundaries into a `DueWorkContract` and a collected
`@due_work_contract_suite(CONTRACT)` class. The adapter does not generate a
contract merely because a normal test uses it. Declare the histories and all
profile dispositions, then execute the generated cases and retain their report;
see [required adoption shape](../ADOPTING.md#required-adoption-shape).
