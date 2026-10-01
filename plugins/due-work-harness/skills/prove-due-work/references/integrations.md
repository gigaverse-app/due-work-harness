# Choose the real failure boundary

These are package capabilities, not blanket support for every application using the named technology. Confirm installed version, client, and setup against the library documentation.

| Stack | Harness boundary | Limit to keep visible |
| --- | --- | --- |
| Django / PostgreSQL | `django_host`: transaction commits, callbacks, selection plans, and generated proofs | Use the app's real database and recovery path. |
| Celery | publication recording/refusal and real worker stages through `celery_worker` | A Celery task send is not proof that the effect is durable. |
| RQ / Redis | RQ worker leases and callbacks; Redis writes and lost replies | Observe the Redis server and subsequent worker, not an in-memory queue copy. |
| Procrastinate / DBOS / django-tasks-db | Real job or workflow transition and documented recovery | Bind the actual worker, restart, or periodic task. |
| MongoDB | `mongodb_host`: acknowledged PyMongo writes, transaction commits, death, lost replies; Motor via `.delegate` | PyMongo 4.9–4.17. Native `AsyncMongoClient`, other processes, and detached tasks are unsupported. [Details](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/mongodb.md). |
| Prefect | `prefect_flow_call` runs the real flow body; `assert_prefect_recurs` checks a supplied deployment schedule | Does not exercise Prefect engine retries, remote workers, or a live deployment. [Details](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/prefect.md). |
| Kafka | `aiokafka` manual consumer offset commits and lost replies with broker restart | Not producer delivery, auto-commit, transactions, rebalances, or other Kafka clients. [Details](https://github.com/gigaverse-app/due-work-harness/blob/main/docs/aiokafka.md). |
| HTTP APIs, including commerce providers | `ExternalCall` or process histories around a real application seam | Model the external response and observable effect faithfully; the provider name alone does not supply an adapter. |

The core accepts plain callables, so a Python application can use it without a named framework integration. The test still needs independent observation and production recovery. The [upstream demonstrations](https://github.com/gigaverse-app/due-work-harness/blob/main/demos/README.md) show measured findings in Saleor, Wagtail, RQ, Celery, DBOS, and Procrastinate. Shopify bulk fixtures and the Airbyte adopter are under development; check their release status before claiming them as installed capabilities.
