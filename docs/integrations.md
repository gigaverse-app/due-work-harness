# Framework integrations

The core accepts plain Python callables and depends on pytest and pydantic,
not on a framework. Optional extras install adapters for failure boundaries a
framework or driver exposes. A test still needs production bindings,
independent observation, and the recovery path the application actually runs.

```python
# conftest.py
from due_work_harness import configure
from due_work_harness.integrations.django import django_host

configure(django_host(production_packages={"myapp"}))
```

| Extra | Scope |
| --- | --- |
| `[django]` | `django_host()`: transaction and commit faults, PostgreSQL plan inspection, lifecycle-state proofs, and pytest-django marks |
| `[celery]` | Beat-schedule evidence, held or refused publications, real worker death points, callback failures, and `worker_contract()` |
| `[procrastinate]` | Worker recovery, stalled-job arrangement, and periodic-task evidence |
| `[dbos]` | Application restart through its own startup for process-level crash histories |
| `[redis]` | `redis_host()`: Redis write/pipeline commit counting and lost write replies |
| `[rq]` | Real RQ worker and recovery, ownership proofs, callback breaker, and `worker_contract()` |
| `[mongodb]` | Acknowledged writes, transaction commits, worker death, and lost replies for PyMongo; Motor uses its delegate. [Boundaries](mongodb.md) |
| `[prefect]` | Real flow-body execution and declared recurrence checks. [Scope](prefect.md) |
| `[aiokafka]` | `AIOKafkaConsumer` acknowledgement and offset-commit faults with broker replay. [Scope](aiokafka.md) |

The harness can test a Shopify integration or any other external API through
the application's real Python call and observed effects. It does not ship a
Shopify-specific adapter or claim every provider behavior is covered. See
[the adopter capability map](adopter-capabilities.md) for what existing suites
actually exercise.
