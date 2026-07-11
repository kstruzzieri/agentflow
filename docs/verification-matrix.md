# Verification Matrix

Choose the narrowest check that can prove the plan step, then broaden when the
blast radius requires it.

| Change type | Focused check | Broader check |
| --- | --- | --- |
| Documentation only | Render or inspect changed docs | Link check when available |
| CLI argument parsing | Command invocation with expected exit code | Full CLI test suite |
| JSON artifact schema | Validate representative valid and invalid fixtures | Full unit test suite |
| Drift audit logic | Temp git repo with scoped and out-of-scope files | Full unit test suite |
| Hunk-level attribution (`audit-drift` / `verify-run` `unmapped_hunks`) | Temp git repo with a recorded hunk and a stray edit in the same allowed file | Full unit test suite; `proof_policy.hunk_attribution` (`enforce`/`observe`/`off`) governs severity — default is `enforce` when an execution contract exists, `off` when none |
| Adaptive review profile (`workflow.contract.json` `review_depth`) | `build-proof`, then read the `required_review_satisfied` check and `review.policy.required_review_depth` | Full unit test suite; `verify-proof --strict` fails when `spec_quality`/`deep` requires a review run and none is recorded, or when the recorded run's `depth_profile` is shallower than required (e.g. a `spec_quality` run against a `deep` requirement); see `docs/recommend-workflow.md` |
| Proof pack generation | Snapshot or content assertions | Full unit test suite |
| Shared library behavior | Targeted unit tests | Type checks and integration tests |
| Dependency change | Lockfile/install validation | Security and license review |
| Browser UI behavior | Browser smoke test | Responsive and interaction checks |

If a check cannot be run, record the reason in the proof pack and list the
remaining risk.
