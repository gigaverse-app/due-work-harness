# Canonical package migration

Keep the implementation in `src/pytest_obligation`, with the legacy namespace
isolated under `shims/`. No duplicate classes, plugin hooks, or harness state.

- [x] New and old adopter declarations generate and execute the same suite.
- [x] Old root and submodule imports expose the canonical contract class.
- [x] Mixed imports share host context and module state; pickle resolves aliases.
- [x] Canonical-only imports do not load shims or optional frameworks.
- [x] Static coverage recognizes both old and new declaration imports.
- [x] Built wheel contains both namespaces and works without the source tree.
- [x] Run core, adopter, integration CI, lint and type checks after the move.

CI owns real-service and upstream-demo verification; consult the PR checks for
their final status. The build lane also runs the compatibility tests against
the installed wheel, with no editable source installation.
