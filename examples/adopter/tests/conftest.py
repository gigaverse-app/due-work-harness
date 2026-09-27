from due_work_harness import Host, configure

# Bindings must reach the application's own code.
configure(Host(production_packages=frozenset({"adopter_app"})))
