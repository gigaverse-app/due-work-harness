# Releasing

Releases follow [Semantic Versioning](https://semver.org/) and are cut from
[Conventional Commit](https://www.conventionalcommits.org/) messages by
[release-please](https://github.com/googleapis/release-please). Nobody edits a
version number by hand.

## How a release happens

1. **Pull requests are squash-merged with their title as the commit message.**
   The `PR title` check requires the title to be a Conventional Commit.
2. **release-please keeps a release PR open** on `main` (`chore(main): release X.Y.Z`),
   holding the next version and the `CHANGELOG.md` entries since the last
   release. Every merge to `main` updates it.
3. **Merging the release PR is the release.** release-please tags the merge
   commit `vX.Y.Z` and creates the GitHub release; the `Release` workflow then
   builds that tag, checks that the built version is the tag, and publishes the
   wheel and sdist to PyPI through Trusted Publishing. The files are also
   attached to the GitHub release.

Each release also moves the floating major tag (`v0` for 0.x) to it, so projects using
`gigaverse-app/pytest-obligation/check@v0` follow the latest compatible release.

The package version is the tag itself (`hatch-vcs`): nothing in `pyproject.toml`
or `uv.lock` changes at release time, and an untagged commit builds as a `.devN`
pre-release that is never published.

## Which version comes next

| Title | Example | Before 1.0 | From 1.0 |
| --- | --- | --- | --- |
| `fix:` | `fix: count commits made through SELECT fn()` | patch | patch |
| `feat:` | `feat: add a SQLAlchemy host` | minor | minor |
| `feat!:` or a `BREAKING CHANGE:` footer | `feat!: rename Delivery.session` | minor | major |
| `perf:`, `revert:` | | patch | patch |
| `docs:`, `refactor:`, `test:`, `build:`, `ci:`, `chore:` | | none | none |

Before 1.0 a breaking change bumps the minor version (`bump-minor-pre-major`),
as SemVer allows for `0.y.z`. `docs:` changes appear in the changelog but do not
release on their own.

## Repository and PyPI settings

The workflow depends on settings outside the code:

1. **PyPI Trusted Publisher.** Before publishing the renamed package, configure
   a pending publisher for PyPI project `pytest-obligation` with
   owner `gigaverse-app`, repository `pytest-obligation`, workflow `release.yml`,
   environment `pypi`. For a project that does not exist yet this is a
   *pending publisher* (pypi.org → Account → Publishing). test.pypi.org has the
   same entry with environment `testpypi` for rehearsals.
   The old project's publisher does not authorize the new project. Do not merge
   the next release PR until this setup is complete. Keep historical releases
   under `due-work-harness`; users migrating to the new distribution must remove
   the old one first because both provide the `due_work_harness` namespace.
2. **GitHub environments.** `pypi` accepts deployments from `main` only, which
   is where both release-please and a manual re-publish run; `testpypi` is
   unrestricted. Adding required reviewers to `pypi` makes every publish wait
   for approval.
3. **Actions may open pull requests** (Settings → Actions → General → Workflow
   permissions), so release-please can open its release PR. The default token
   stays read-only; jobs ask for what they need.
4. **Squash merging only, titled by the PR title** (Settings → General → Pull
   Requests), so each commit on `main` is the checked title.

Release PRs are opened with the workflow's token, and GitHub does not run
workflows for events created by it, so CI does not run on the release PR
itself. It only changes `CHANGELOG.md` and the version manifest; the
commit it releases already passed CI when it was merged.

## Rehearsing or re-publishing

`Actions → Release → Run workflow` builds an existing tag and publishes it to
TestPyPI (the default) or PyPI. Use it to rehearse before the first release, or
to finish a release whose publish step failed after the tag was created.
