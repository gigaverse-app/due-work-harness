from due_work_harness import configure
from due_work_harness.integrations.django import django_host

# The system under test is the upstream demo, which is part of the procrastinate
# package, running on Django: bindings must reach one of them.
configure(django_host(production_packages={"procrastinate", "django"}, lifecycle_proofs=False))
