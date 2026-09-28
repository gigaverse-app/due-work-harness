import pytest

from due_work_harness import configure
from due_work_harness.integrations.celery import celery_publication_breaker, celery_publications
from due_work_harness.integrations.django import django_host

# The Django references stand in for production in the harness's own tests;
# they live under the harness package, which the tripwires treat as root-owned.
configure(
    django_host(
        production_packages=set(),
        publication_recorder=celery_publications,
        publication_breaker=celery_publication_breaker,
    )
)


@pytest.fixture(autouse=True)
def _reset_host_between_tests() -> None:
    configure(
        django_host(
            production_packages=set(),
            publication_recorder=celery_publications,
            publication_breaker=celery_publication_breaker,
        )
    )
