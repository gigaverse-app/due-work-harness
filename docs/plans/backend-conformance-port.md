# Backend conformance improvements

Port the reusable improvements from gigaverse-backend #5128 and #5142 (source
commit f933d6075) into the public harness. Preserve the OSS host boundary,
validated Pydantic models, Python 3.12 support, existing integration capabilities,
crash findings, process histories and verification guards.

- [ ] Add deterministic in-flight and evidence-confluence histories, replay,
      portable provider controls, negative controls, and optional Hypothesis.
- [ ] Unify declarations around profiles A–J with canonical names, independent
      E-family assessments and explicit strict NotAssessed XFAILs.
- [ ] Reuse gate/replay/retry proofs for G/H/J; expose portable partial-admission
      rollback proofs for I without importing backend or framework models.
- [ ] Add executed profile coverage reporting that respects filtering, teardown,
      repeated attempts, xdist and explicit assessment debt.
- [ ] Migrate package exports, self-tests, examples, demos and adoption docs;
      preserve existing real failure detection and static enrollment checks.
- [ ] Verify minimal core without frameworks/Hypothesis, optional exploration,
      relevant integrations, lint/types, wheel contents and CI.

No backend application adapters or runtime policies are copied into the public
package. The backend continues using its local harness; adopting the OSS package
there is a separate change.
