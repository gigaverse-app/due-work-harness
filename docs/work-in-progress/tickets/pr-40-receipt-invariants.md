# PR 40 receipt invariant checks

- [x] Reject premature confirmation after every observable recovery turn, including bounded settlement; prove failure before the fix and successful trace replay afterward.
- [x] Generate opt-in receipt faults for creation, updates, and return to an earlier value; correct writers pass and update-only counterfeits fail, with replay-safe and replay-unsafe controls.
- [x] Reuse the reference writer and one runner receipt-check owner; preserve catalogs for declarations that do not opt in and the documented retirement limitation.
- [x] Reject recovery that silently changes the commanded revision, with a failing-before/passing-after replay-unsafe control.
- [x] Exercise public pytest collection/execution and the runnable SQLite adopter, plus core, exploration, compatibility, lint and targeted type checks.

Review and verification results, including hosted CI status for the published
revision, are recorded in PR 40 rather than checked-in execution artifacts.

This is verification of the testing library and its supported pytest entry point. It does not claim to verify a deployed Shopify billing integration or Shopify's rejected-key behavior.
