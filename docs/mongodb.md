# MongoDB histories

Install `due-work-harness[mongodb]` and configure
`mongodb_host(client, production_packages={"your_app"})`. For Motor, pass the
Motor client's `.delegate`; run async application calls to completion on the
same event loop your client uses.

The host counts acknowledged wire writes, including `findAndModify`, bulk
batches, `$out`/`$merge`, and explicit transaction commits. Transactional writes
are not durable boundaries until commit. Reads and aborted transactions do not
count. Each bulk wire batch is one boundary; a batch can contain multiple
documents. A batch that inserts some documents before reporting a duplicate-key
error still exposes its acknowledged boundary. Unknown commands count conservatively. Writes using `w=0` are refused before dispatch, including ordered bulks whose
driver internally converts them into acknowledged commands. `$out` and `$merge`
have real-server controls alongside read-only aggregation.

Death fences subsequent commands on that client, including commands issued
from Motor executor threads and finally blocks. Independent clients remain
available to observe persisted state. Lost replies let the write land and raise
PyMongo `ConnectionFailure`; normal driver and application error handling runs.
Configure driver retries as production does: they are part of that behavior.

The optional dependency bounds PyMongo below 4.18, which changed the wire API.
Native `AsyncMongoClient` is not supported; it is rejected instead of silently
producing empty histories. Other clients, detached tasks and other processes
are outside this host's scope. Server TTL time is not frozen. This host does
not supply query-plan, schedule, or transport evidence; configure those separately.

The real-server suite includes a transaction control, Motor thread control,
lost acknowledgements, blocked cleanup and generated outbox histories. Removing
provider deduplication makes the same generated history detect a duplicate effect.

## Required adoption surface

Use this host inside a `DueWorkContract` adoption with a collected
`@due_work_contract_suite(CONTRACT)` class. The adapter supplies fault injection;
it does not replace the contract or generate cases on its own. See
[required adoption shape](../ADOPTING.md#required-adoption-shape) for declarations
and generated test/report evidence.
