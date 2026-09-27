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

## One-time setup

These are repository and PyPI settings, so they are done by a maintainer, not
by the workflow:

1. **PyPI Trusted Publisher.** On pypi.org, under *Publishing*, add a pending
   publisher for project `due-work-harness`: owner `gigaverse-app`, repository
   `due-work-harness`, workflow `release.yml`, environment `pypi`. Do the same on
   test.pypi.org with environment `testpypi` to allow rehearsals.
2. **GitHub environments** `pypi` and `testpypi` (Settings → Environments).
   Adding required reviewers to `pypi` makes every publish wait for approval.
3. **Let Actions open pull requests** (Settings → Actions → General → Workflow
   permissions → *Allow GitHub Actions to create and approve pull requests*), so
   release-please can open its release PR.
4. **Squash merging with the PR title** (Settings → General → Pull Requests:
   allow squash merging only, default commit message *Pull request title*), so
   each commit on `main` is the checked title.

Release PRs are opened with the workflow's token, and GitHub does not run
workflows for events created by it, so CI does not run on the release PR
itself. It only changes `CHANGELOG.md`, `version.txt` and the manifest; the
commit it releases already passed CI when it was merged.

## Rehearsing or re-publishing

`Actions → Release → Run workflow` builds an existing tag and publishes it to
TestPyPI (the default) or PyPI. Use it to rehearse before the first release, or
to finish a release whose publish step failed after the tag was created.
