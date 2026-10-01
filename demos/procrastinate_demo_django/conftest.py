from pytest_obligation import configure
from pytest_obligation.integrations.django import django_host

# The system under test is the upstream demo, which is part of the procrastinate
# package, running on Django: bindings must reach one of them.
configure(django_host(production_packages={"procrastinate", "django"}, lifecycle_proofs=False))
