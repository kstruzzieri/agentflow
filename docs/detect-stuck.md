# detect-stuck

`agentflow detect-stuck` flags a narrow, deterministic class of stuck-agent
behavior from the `.agent/` ledgers — no model conversation required. It is
read-only and writes nothing.

## Rules

- `repeated_command_failure` — the same command (argv + gate + cwd) fails N or
  more times within one `(step_id, attempt_id)` with no successful run in
  between. Default N = 3.
- `repeated_verify_failure` — M or more consecutive step-scope verification
  failures within one `(step_id, attempt_id)` with no file-change receipt
  landing between the first and last failure. Default M = 2.
- `alternating_no_op` — an exact command cycle of period 2 or 3 repeats K or
  more times within one `(step_id, attempt_id)` while no files change and no
  verification passes. Default K = 3.

## Usage

    agentflow detect-stuck [--json] [--strict]
                           [--min-command-failures N]
                           [--min-verify-failures M]
                           [--min-cycle-repeats K]

Exit code is 0 by default, even when findings exist. With `--strict`, the
command exits 1 when any finding is present (for CI gating).

`verify-run` surfaces stuck findings as advisory warnings, and `build-proof`
records them in a top-level `stuck` block. Both are advisory-only: they never
fail the host command, even under that command's own `--strict`. The proof
`stuck` block is covered by the proof core hash but is not a gated check.

## Limitations

- Receipt-pattern detection only — not conversation or semantic analysis.
- Scoped within a single `attempt_id`; cross-attempt and amendment-retry loops
  are not flagged (deferred to a future version).
- Invisible to unrecorded work: commands and file changes that are never
  recorded as receipts cannot be analyzed.
- `.agent/failures.jsonl` signatures are not correlated in this version.
- No-net-diff loops (edit-then-revert with file receipts present) are not
  caught by the zero-file-receipt rules (deferred).
- Thresholds are heuristic; tune them with the flags above.
