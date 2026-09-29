# Changelog

## [0.5.2](https://github.com/gigaverse-app/due-work-harness/compare/v0.5.1...v0.5.2) (2026-09-29)


### Documentation

* the upstream playbook and a skill, for repeating the find-and-disclose cycle ([#21](https://github.com/gigaverse-app/due-work-harness/issues/21)) ([9cc0639](https://github.com/gigaverse-app/due-work-harness/commit/9cc06397d36ca53c15bfb06a11cb6e12f3b5f7cb))

## [0.5.1](https://github.com/gigaverse-app/due-work-harness/compare/v0.5.0...v0.5.1) (2026-09-29)


### Bug fixes

* **django:** run the autocommit-write probe through a DB-API cursor, so psycopg2 works ([#22](https://github.com/gigaverse-app/due-work-harness/issues/22)) ([d383885](https://github.com/gigaverse-app/due-work-harness/commit/d383885e2e367c42a53eb227be945aaca98df13f))

## [0.5.0](https://github.com/gigaverse-app/due-work-harness/compare/v0.4.0...v0.5.0) (2026-09-28)


### Features

* **contract:** histories declare their findings, one run per history set ([#19](https://github.com/gigaverse-app/due-work-harness/issues/19)) ([301ada7](https://github.com/gigaverse-app/due-work-harness/commit/301ada78f358bfc0f36c320bbf95cf2d06b26348))

## [0.4.0](https://github.com/gigaverse-app/due-work-harness/compare/v0.3.0...v0.4.0) (2026-09-28)


### Features

* **process-histories:** one child fault protocol for every process history ([5835ac1](https://github.com/gigaverse-app/due-work-harness/commit/5835ac13ded76360f8a7ba4dc24f81f72a5d2653))
* **pytest:** --due-work-summary lists what each generated suite produced ([5835ac1](https://github.com/gigaverse-app/due-work-harness/commit/5835ac13ded76360f8a7ba4dc24f81f72a5d2653))

## [0.3.0](https://github.com/gigaverse-app/due-work-harness/compare/v0.2.0...v0.3.0) (2026-09-28)


### Features

* **integrations:** Redis, RQ and Celery's real worker, with lost-reply and survivable-failure histories ([#15](https://github.com/gigaverse-app/due-work-harness/issues/15)) ([93ba16d](https://github.com/gigaverse-app/due-work-harness/commit/93ba16dd697f8202af32b987b06b2dc7623b3f18))


### Bug fixes

* **demos:** the Saleor run's version fallback actually applies ([6e1cf41](https://github.com/gigaverse-app/due-work-harness/commit/6e1cf4189c6c0f241648eb1e386cb37f9eb3b8b5))

## [0.2.0](https://github.com/gigaverse-app/due-work-harness/compare/v0.1.1...v0.2.0) (2026-09-28)


### Features

* assert_pinned_outcomes for findings tables, and due_work_database for hand-written tests ([c18e37b](https://github.com/gigaverse-app/due-work-harness/commit/c18e37bc4bc15be26c41272c02dd173a4997b30b))
* **crash-histories:** fail each signal receiver in turn ([c18e37b](https://github.com/gigaverse-app/due-work-harness/commit/c18e37bc4bc15be26c41272c02dd173a4997b30b))
* **demos:** Wagtail 8.0 on django-tasks-db, unmodified from PyPI ([c18e37b](https://github.com/gigaverse-app/due-work-harness/commit/c18e37bc4bc15be26c41272c02dd173a4997b30b))
* **django-tasks:** django-tasks-db integration, its worker as recovery and its own worker contract ([c18e37b](https://github.com/gigaverse-app/due-work-harness/commit/c18e37bc4bc15be26c41272c02dd173a4997b30b))
* **django:** serialized_rollback for projects whose migrations seed rows ([c18e37b](https://github.com/gigaverse-app/due-work-harness/commit/c18e37bc4bc15be26c41272c02dd173a4997b30b))


### Bug fixes

* **celery:** count a non-eager publication once ([c18e37b](https://github.com/gigaverse-app/due-work-harness/commit/c18e37bc4bc15be26c41272c02dd173a4997b30b))


### Documentation

* embed the explainer and findings videos in the README ([#11](https://github.com/gigaverse-app/due-work-harness/issues/11)) ([f320002](https://github.com/gigaverse-app/due-work-harness/commit/f320002c7b26471c6e23b3e0f564dc9ab975f101))

## [0.1.1](https://github.com/gigaverse-app/due-work-harness/compare/v0.1.0...v0.1.1) (2026-09-28)


### Bug fixes

* **verify:** only the xdist controller verifies declared suites ([c628f16](https://github.com/gigaverse-app/due-work-harness/commit/c628f16492251733bf9f2bd75f1cf1fb4b983b20))


### Documentation

* a recorded demo GIF of the harness finding real bugs, and a README rewrite ([#8](https://github.com/gigaverse-app/due-work-harness/issues/8)) ([3e2f6a0](https://github.com/gigaverse-app/due-work-harness/commit/3e2f6a0a285143a87c1859865d0c4f9a83a58851))

## 0.1.0 (2026-09-28)


### Features

* **coverage:** find Dramatiq, RQ and Django task handoffs, and Celery's on-commit sends ([f03622f](https://github.com/gigaverse-app/due-work-harness/commit/f03622fd93d1af71b78a541d47e1a18b7cd18123))
* **coverage:** scan source roots outside the project's directory ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* **crash-histories:** fail each after-commit callback in turn ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* **crash-histories:** the broker refuses each publication in turn ([df8b17f](https://github.com/gigaverse-app/due-work-harness/commit/df8b17f62f44ed0db1abd2ffd862efbb19392d96))
* **demos:** DBOS and Saleor claim what their frameworks provide ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* **demos:** every demo adopts the harness through contracts ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* **demos:** Saleor claims recovery, its selection observed from the tick it runs ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* **demos:** Saleor's checkout, unmodified, loses charges and confirmations ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* **ownership:** prove every owner write is fenced, not only the settlement ([df8b17f](https://github.com/gigaverse-app/due-work-harness/commit/df8b17f62f44ed0db1abd2ffd862efbb19392d96))
* **procrastinate:** claim ownership, retention, replay and retry against procrastinate itself ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* publish to PyPI with SemVer releases cut from Conventional Commits ([#2](https://github.com/gigaverse-app/due-work-harness/issues/2)) ([8156135](https://github.com/gigaverse-app/due-work-harness/commit/8156135a433683d34848afbca89dfcee82b4af75))


### Bug fixes

* **check-action:** keep the check's Python out of the caller's job ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* **contract:** probes run with the contract's database; rewritten asserts are not inversions ([1c8492d](https://github.com/gigaverse-app/due-work-harness/commit/1c8492df3f8e45c7e34ca201f0a9169fe7ec1e55))
* **procrastinate:** poll for a reclaimed job while it is due within the skew ([df8b17f](https://github.com/gigaverse-app/due-work-harness/commit/df8b17f62f44ed0db1abd2ffd862efbb19392d96))
