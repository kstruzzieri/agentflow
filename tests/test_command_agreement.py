"""#108: the commands that judge one ledger must agree about it.

Since 1.0, seven bugs were two Agentflow commands disagreeing about the same
ledger state (#58 #60 #70 #73 #81 #97 #103), and #94 made next-action blame
the ledger for its own error. Each was found on its own. This harness builds
ledger states through the CLI and, from each, follows next-action to a
complete run. At every step of that walk it checks that:

1. the command next-action recommends is accepted;
2. next-action reports the run finishable (it recommends finish-run, or says
   complete) if and only if verify-run and finish-run pass;
3. the walk reaches complete without recommending the same (state, args,
   attempt) twice;
4. next-action never answers state_invalid;
5. a step obligation next-action names is one a judge enforces: an attempt it
   says cannot complete is refused by complete-step and finish-step (#70,
   #81), every receipt it asks for is one a verify-step or verify-run error
   names (#97), and an amendment it asks for repairs something verify-run
   rejects;
6. once the run is complete, reopening a step asks to record only content its
   latest completed receipt already verified, so no unverified edit hides
   behind a passing verify-run (#103).

These hold for the catalog's domain: ledgers built through the CLI, in-scope
edits, one agent, gates that pass once rerun. Outside it some states mean
otherwise on purpose: drift_failing recommends finish-run to report an
out-of-scope edit, and two open attempts are state_invalid.

Each catalog state runs in default and --strict mode. Probes that write
(finish-run, complete-step, finish-step, amend-step) run on a copy of the
root, so the walk itself runs only what next-action recommends.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentflow import cli, execution_coverage, porcelain
from agentflow.contracts import PLAN_SCHEMA_VERSION
from agentflow.execution import read_step_state
from agentflow.git import changed_file_records
from agentflow.receipts import file_receipts


AGENT = "tester"
GATE = 'python3 -c "print(1)"'
PASS = ["python3", "-c", "print(1)"]


def af_out(root: Path, *args: str):
    """Run one agentflow command against root; return its exit code and output."""
    argv = list(args)
    cut = argv.index("--") if "--" in argv else len(argv)
    argv[cut:cut] = ["--root", str(root)]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        return cli.main(argv), out.getvalue().strip()


def af(root: Path, *args: str) -> int:
    return af_out(root, *args)[0]


@contextlib.contextmanager
def copy_of(root: Path):
    """A throwaway copy of root, git history and .agent/ included."""
    with TemporaryDirectory() as d:
        copy = Path(d) / "repo"
        shutil.copytree(root, copy, symlinks=True)
        yield copy


def set_contract(root: Path, section: str, key: str, value) -> None:
    path = root / ".agent/execution.contract.json"
    contract = json.loads(path.read_text())
    contract[section][key] = value
    path.write_text(json.dumps(contract))


# Ledger building blocks. Every one goes through the CLI.

def repo(root: Path, steps=(("P1", ("src/feature.py",)),), gate=PASS, policy="enforce") -> None:
    root.mkdir()
    # No commits: every fixture file is untracked, so a global excludes file must not hide it.
    for args in (["init", "-q"], ["config", "core.excludesFile", os.devnull]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    assert af(root, "init") == 0
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "command agreement fixture",
        "scope": ["exercise the execution loop"],
        "non_goals": [],
        "invariants": ["tests pass"],
        "allowed_files": [".agent/", *sorted({path for _id, paths in steps for path in paths})],
        "blocked_files": [],
        "validation_gates": [GATE],
        "rollback_plan": "git reset",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0, "new_dependencies": 0, "formatting_drift": "minimal",
            "architecture_drift": "requires_approval", "test_weakening": 0,
        },
        "evidence_ids": [],
        "locked": False,
        "locked_at": None,
        "steps": [
            {
                "id": step_id, "action": f"{step_id} edits its files.", "files": list(paths),
                "preconditions": [], "expected_diff": [f"{step_id} edits its files."],
                "validation": [GATE], "gates": [{"kind": "command", "run": list(gate)}],
                "evidence_ids": [],
            }
            for step_id, paths in steps
        ],
    }
    path = root / ".agent/plan.lock.json"
    path.write_text(json.dumps(plan))
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main(["lock-plan", str(path)]) == 0
    assert af(root, "init-execution") == 0
    for step_id, _paths in steps:
        # Strict build-proof fails a step with no planning evidence (steps_without_support).
        assert af(root, "record-evidence", "--id", f"E-{step_id}", "--claim", f"{step_id} is planned",
                  "--source", "plan", "--supports", step_id) == 0
    set_contract(root, "proof_policy", "hunk_attribution", policy)


def claim(root: Path, step_id: str = "P1") -> None:
    assert af(root, "claim-step", step_id, "--agent", AGENT) == 0


def amend(root: Path, step_id: str = "P1") -> None:
    assert af(root, "amend-step", step_id, "--agent", AGENT, "--reason", "fixture") == 0


def close(root: Path, how: str, step_id: str = "P1") -> None:
    assert af(root, how, step_id, "--agent", AGENT, "--reason", "fixture") == 0


def edit(root: Path, step_id: str = "P1", path: str = "src/feature.py", text: str = "x = 1\n") -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)
    assert af(root, "record-file-change", "--step", step_id, "--path", path, "--agent", AGENT) == 0


def gate(root: Path, step_id: str = "P1", command=PASS) -> int:
    return af(root, "run", "--step", step_id, "--gate", GATE, "--agent", AGENT, "--", *command)


def time_out(root: Path, step_id: str = "P1") -> None:
    contract = json.loads((root / ".agent/execution.contract.json").read_text())
    limit = contract["command_policy"]["command_timeout_seconds"]
    set_contract(root, "command_policy", "command_timeout_seconds", 1)
    assert gate(root, step_id, ["python3", "-c", "import time; time.sleep(5)"]) == 124
    set_contract(root, "command_policy", "command_timeout_seconds", limit)


def finish(root: Path, step_id: str = "P1") -> None:
    assert gate(root, step_id) == 0
    assert af(root, "finish-step", step_id, "--agent", AGENT) == 0


def completed(root: Path, **repo_args) -> None:
    repo(root, **repo_args)
    claim(root)
    edit(root)
    finish(root)


# The catalog: one builder per ledger state. A "#n" name is that bug's shape.

def unclaimed(root):  # its walk's second state is a fresh claim
    repo(root)


def open_attempt_with_passing_gate(root):  # #58
    repo(root)
    claim(root)
    edit(root)
    assert gate(root) == 0


def stopped_attempt(how):
    def build(root):
        repo(root)
        claim(root)
        edit(root)
        close(root, how)
    return build


def timed_out_attempt(root):  # #70
    repo(root)
    claim(root)
    edit(root)
    time_out(root)


def passing_rerun_in_timed_out_attempt(root):  # #70
    timed_out_attempt(root)
    assert gate(root) == 0


def superseded_timeout(root):  # #60: A1 timed out and failed, A2 completed
    timed_out_attempt(root)
    close(root, "fail-step")
    claim(root)
    edit(root)
    finish(root)


def stale_verified_mark(root):  # #81: verify-step passed, then the gate failed
    flag = root.parent / "fail-once"
    fail_once = ["python3", "-c", f"import os, sys; p = {str(flag)!r}; f = os.path.exists(p); "
                                  "f and os.remove(p); sys.exit(f)"]
    repo(root, gate=fail_once)
    claim(root)
    edit(root)
    assert gate(root, command=fail_once) == 0
    assert af(root, "verify-step", "P1", "--agent", AGENT) == 0
    flag.touch()
    assert gate(root, command=fail_once) == 1  # removes the flag: the next run passes


def timed_out_amendment(root):
    completed(root)
    amend(root)
    time_out(root)


def closed_amendment(how, text=None):  # #103 when blocked with an edit
    def build(root):
        completed(root)
        amend(root)
        if text is not None:
            edit(root, text=text)
            assert gate(root) == 0
        close(root, how)
    return build


def post_completion_edit(policy):  # #73
    def build(root):
        completed(root, policy=policy)
        (root / "src/feature.py").write_text("x = 2\n")
    return build


def shared_file_recorded_by_another_step(root):  # #97
    repo(root, steps=(("P1", ("src/a.py", "src/shared.py")), ("P2", ("src/shared.py",))))
    claim(root, "P2")
    edit(root, "P2", "src/shared.py")
    finish(root, "P2")
    claim(root, "P1")
    edit(root, "P1", "src/a.py")
    assert gate(root, "P1") == 0


def two_step_plan_part_way(root):
    repo(root, steps=(("P1", ("src/a.py",)), ("P2", ("src/b.py",))))
    claim(root)
    edit(root, path="src/a.py")
    finish(root)


CATALOG = {
    "unclaimed": unclaimed,
    "#58 open attempt with passing gate": open_attempt_with_passing_gate,
    "blocked attempt with edit": stopped_attempt("block-step"),
    "failed attempt with edit": stopped_attempt("fail-step"),
    "#70 timed-out open attempt": timed_out_attempt,
    "#70 passing rerun in timed-out attempt": passing_rerun_in_timed_out_attempt,
    "#60 superseded timeout": superseded_timeout,
    "#81 stale verified mark": stale_verified_mark,
    "timed-out amendment": timed_out_amendment,
    "failed amendment": closed_amendment("fail-step"),
    "blocked amendment": closed_amendment("block-step"),
    "failed amendment with edit": closed_amendment("fail-step", "x = 2\n"),
    "#103 blocked amendment with edit": closed_amendment("block-step", "x = 2\n"),
    # Documented (docs/agent-workflow.md, verify-step and verify-run): a reopened
    # step re-records the file, though verify-run accepts the matching content.
    "blocked amendment re-recording completed content": closed_amendment("block-step", "x = 1\n"),
    "#73 post-completion edit (enforce)": post_completion_edit("enforce"),
    "#73 post-completion edit (observe)": post_completion_edit("observe"),
    "#73 post-completion edit (off)": post_completion_edit("off"),
    "#97 shared file recorded by another step": shared_file_recorded_by_another_step,
    "two-step plan part-way": two_step_plan_part_way,
}


class TestCommandAgreement(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"AGENTFLOW_STRICT": "", "AGENTFLOW_AGENT_ID": ""})
        env.start()
        self.addCleanup(env.stop)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            os.environ.pop(name, None)  # e.g. set by a git hook running the suite

    def walk(self, root: Path, strict: bool) -> None:
        """Follow next-action to complete, checking agreement before each command.

        finish-run runs audit-drift, verify-run, build-proof and verify-proof, so
        when next-action recommends it, the walk's own finish-run is the probe:
        it passes exactly when verify-run and finish-run both pass (check 2).
        """
        strict_args = ["--strict"] if strict else []
        seen = set()
        last = None
        for _ in range(20):
            action = porcelain.next_action(root, strict=strict, agent_id=AGENT)
            self.assertNotEqual(action.state, "state_invalid", action.diagnostics)  # 4
            if action.state == "complete":
                code, out = af_out(root, "verify-run", "--no-record", *strict_args)
                self.assertEqual(code, 0, f"next-action says complete but verify-run fails: {out}")
                if last != ["finish-run", *strict_args]:
                    with copy_of(root) as copy:
                        code, out = af_out(copy, "finish-run", *strict_args)
                    self.assertEqual(code, 0, f"next-action says complete but finish-run fails: {out}")
                self.check_reopen(root, strict)  # 6
                return
            attempt = ((action.resumability or {}).get("attempt") or {}).get("id")
            key = (action.state, tuple(action.args), attempt)
            self.assertNotIn(key, seen, f"next-action repeated {action.state}: {action.command}")  # 3
            seen.add(key)
            self.assertTrue(action.args, f"{action.state} offers no command: {action.diagnostics}")
            if action.args[0] != "finish-run":
                self.check_unfinishable(root, action, strict)  # 2, 5
            code, out = af_out(root, *action.args)  # 1, and 2 for finish-run
            self.assertEqual(code, 0, f"{action.command} refused the command next-action "
                                      f"recommended at {action.state} {action.diagnostics}: {out}")
            last = action.args
        self.fail("next-action did not reach complete in 20 commands")

    def check_unfinishable(self, root: Path, action, strict: bool) -> None:
        """next-action has work before finish-run, so the run judges must refuse."""
        strict_args = ["--strict"] if strict else []
        run_ok = af(root, "verify-run", "--no-record", *strict_args) == 0
        if run_ok:  # finish-run includes verify-run, so it needs probing only now
            with copy_of(root) as copy:
                self.assertNotEqual(af(copy, "finish-run", *strict_args), 0,
                                    f"finish-run passes but next-action says {action.state}: "
                                    f"{action.command}")
        step_id = action.step_id
        if action.state in ("validation_missing", "attempt_timed_out"):
            # A refusal writes no step event, so both probes share one copy.
            # complete-step has no --strict; it reads AGENTFLOW_STRICT.
            strict_env = {"AGENTFLOW_STRICT": "1" if strict else ""}
            with copy_of(root) as copy, mock.patch.dict(os.environ, strict_env):
                for argv in (["complete-step", step_id, "--agent", AGENT],
                             ["finish-step", step_id, "--agent", AGENT, *strict_args]):
                    self.assertNotEqual(af(copy, *argv), 0,
                                        f"{argv[0]} accepted an attempt next-action says is at "
                                        f"{action.state}: {action.diagnostics}")
        if action.state == "file_receipts_missing":
            plan = json.loads((root / ".agent/plan.lock.json").read_text())
            attempt_id = action.resumability["attempt"]["id"]
            step = execution_coverage.verify_step(root, plan, step_id, attempt_id,
                                                  strict=strict, record=False)
            run = execution_coverage.verify_run(root, plan, strict=strict, record=False)
            errors = [f["message"] for f in [*step["errors"], *run["errors"]]]
            for line in action.diagnostics:
                if line.startswith("unrecorded: "):
                    path = line[len("unrecorded: "):]
                    self.assertTrue(any(path in message.split() for message in errors),
                                    f"next-action asks {step_id} to record {path}, which no "
                                    f"verify-step or verify-run error names: {errors}")
        if action.state == "step_amendment_needed":
            self.assertFalse(run_ok, f"next-action asks for an amendment verify-run does not need: "
                                     f"{action.diagnostics}")

    def check_reopen(self, root: Path, strict: bool) -> None:
        """A reopened step may re-record only content a completed attempt verified.

        docs/agent-workflow.md: verify-step wants a replacement to record a file a
        stopped attempt last recorded; verify-run accepts that file when it matches
        its latest completed receipt. Anything else here is an edit verify-run let
        through unverified.
        """
        plan = json.loads((root / ".agent/plan.lock.json").read_text())
        for step in plan["steps"]:
            with copy_of(root) as copy:
                amend(copy, step["id"])
                action = porcelain.next_action(copy, strict=strict, agent_id=AGENT)
                if action.state != "file_receipts_missing":
                    continue
                attempts = read_step_state(copy)["attempts"]
                verified = {
                    receipt["path"]: receipt for receipt in file_receipts(copy)
                    if attempts.get(receipt.get("attempt_id"), {}).get("status") == "completed"
                }
                changed = {record["path"]: record for record in changed_file_records(copy)}
                for line in action.diagnostics:
                    if line.startswith("unrecorded: "):
                        path = line[len("unrecorded: "):]
                        self.assertTrue(
                            path in verified and path in changed and
                            execution_coverage.receipt_matches_worktree(copy, changed[path], verified[path]),
                            f"a complete run reopened at {step['id']} holds {path}, which no "
                            f"completed attempt verified as it is now",
                        )

    def test_every_ledger_state_walks_to_complete_in_agreement(self):
        for name, build in CATALOG.items():
            with self.subTest(state=name), TemporaryDirectory() as d:
                built = Path(d) / "built"
                build(built)
                for strict in (False, True):
                    with self.subTest(strict=strict), copy_of(built) as root:
                        self.walk(root, strict)

    def test_94_an_internal_fault_surfaces_instead_of_asking_for_a_rerun(self):
        # #94 answered state_invalid ("ledgers changed; run it again") for its own
        # error, so every rerun gave the same answer. The fault must surface.
        for name in ("unclaimed", "#58 open attempt with passing gate"):
            with self.subTest(state=name), TemporaryDirectory() as d:
                root = Path(d) / "repo"
                CATALOG[name](root)
                fault = TypeError("simulated defect")
                with mock.patch.object(porcelain, "_selected_action", side_effect=fault):
                    with self.assertRaises(TypeError) as raised:
                        self.walk(root, strict=False)
                self.assertIs(raised.exception, fault)


if __name__ == "__main__":
    unittest.main()
