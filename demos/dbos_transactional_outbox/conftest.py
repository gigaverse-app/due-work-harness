from due_work_harness import Host, configure

# The system under test is the upstream demo module and DBOS itself.
configure(Host(production_packages=frozenset({"dbos", "transactional-outbox"})))
