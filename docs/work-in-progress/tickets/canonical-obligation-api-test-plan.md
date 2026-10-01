# Canonical package migration

Keep the implementation in `src/pytest_obligation`, with the legacy namespace
isolated under `shims/`. No duplicate classes, plugin hooks, or harness state.

- [ ] New and old adopter declarations generate and execute the same suite.
- [ ] Old root and submodule imports expose the canonical contract class.
- [ ] Mixed imports share host context and module state; pickle resolves aliases.
- [ ] Canonical-only imports do not load shims or optional frameworks.
- [ ] Static coverage recognizes both old and new declaration imports.
- [ ] Built wheel contains both namespaces and works without the source tree.
- [ ] Run core, adopter, integration CI, lint and type checks after the move.
