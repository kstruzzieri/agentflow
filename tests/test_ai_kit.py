"""Keep the AI review kit's cross-references honest.

`docs/ai/` holds four things that point at each other: an index
(`ai-kit.yaml`), an orientation doc (`PROJECT-CONTEXT.md`), the machine policy
the runner loads (`config.json`), and its human-readable mirror
(`config.yaml`). `test_config_contract.py` already pins `config.json` internally.
What nothing pinned before is that the mirror still agrees with it, and that the
paths these files name still exist.

The tree ships no YAML parser, so the YAML is read as text. That is enough: the
failure worth catching is a reference or a mirrored value going stale, not a
typed schema violation.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AI_DIR = ROOT / "docs/ai"
KIT = AI_DIR / "ai-kit.yaml"
CONTEXT = AI_DIR / "PROJECT-CONTEXT.md"
CONFIG_JSON = AI_DIR / "config.json"
CONFIG_YAML = AI_DIR / "config.yaml"

# Repo-relative path-shaped tokens: a slash-separated name ending in a known
# suffix, or one of the two bare directories the kit names.
PATH_TOKEN = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.(?:md|json|yaml|py)"
    r"|docs/ai/prompts|docs/ai/state)(?![\w/-])"
)

# Written at run time and gitignored, so absence in a clean tree is expected.
RUNTIME_ROOTS = (".agent/", "docs/ai/state")


def _uncommented(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _unresolved(text: str) -> list[str]:
    return sorted(
        {
            token
            for token in PATH_TOKEN.findall(_uncommented(text))
            if not token.startswith(RUNTIME_ROOTS) and not (ROOT / token).exists()
        }
    )

def _yaml_list(text: str) -> list[str]:
    return [item.strip().strip("\"'") for item in text.split(",") if item.strip()]


def _mirrored_gate_policy() -> dict[str, dict[str, list[str]]]:
    """Extract gate_policy from config.yaml without a YAML parser."""
    text = CONFIG_YAML.read_text(encoding="utf-8")
    section = text.split("\ngate_policy:", 1)
    if len(section) != 2:
        return {}
    body = section[1]
    policy: dict[str, dict[str, list[str]]] = {}
    gate = None
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break  # next top-level key
        name = re.match(r"^  ([a-z_]+):\s*$", line)
        if name:
            gate = name.group(1)
            policy[gate] = {}
            continue
        entry = re.match(r"^\s+(blocks_on|warns_on):\s*\[([^\]]*)\]\s*$", line)
        if entry and gate:
            policy[gate][entry.group(1)] = _yaml_list(entry.group(2))
    return policy


def _mirrored_branch_gates() -> dict[str, str]:
    text = CONFIG_YAML.read_text(encoding="utf-8")
    section = text.split("\nbranch_modifiers:", 1)
    if len(section) != 2:
        return {}
    gates: dict[str, str] = {}
    branch = None
    for line in section[1].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) == 0:
            break
        name = re.match(r"""^  "?([^":]+)"?:\s*$""", line)
        if name:
            branch = name.group(1)
            continue
        gate = re.match(r"^\s+gate:\s*(\S+)\s*$", line)
        if gate and branch:
            gates[branch] = gate.group(1).strip("\"'")
    return gates


class AiKitCrossReferenceTests(unittest.TestCase):
    def test_index_and_context_exist(self) -> None:
        self.assertTrue(KIT.is_file(), KIT)
        self.assertTrue(CONTEXT.is_file(), CONTEXT)

    def test_every_path_the_index_names_exists(self) -> None:
        self.assertEqual(_unresolved(KIT.read_text(encoding="utf-8")), [])

    def test_every_link_the_context_names_resolves(self) -> None:
        # Markdown links in PROJECT-CONTEXT.md are relative to docs/ai/.
        missing = sorted(
            {
                target
                for target in re.findall(
                    r"\]\(([^)#:]+)\)", CONTEXT.read_text(encoding="utf-8")
                )
                if not (AI_DIR / target).resolve().exists()
            }
        )

        self.assertEqual(missing, [])

    def test_index_does_not_restate_the_gate_policy(self) -> None:
        # A third copy of the policy would be a third thing to drift.
        self.assertNotIn("gate_policy", KIT.read_text(encoding="utf-8"))
        self.assertNotIn("blocks_on", KIT.read_text(encoding="utf-8"))

    def test_index_records_the_stdlib_only_test_command(self) -> None:
        text = KIT.read_text(encoding="utf-8")

        self.assertIn("PYTHONPATH=src python3 -m unittest discover -s tests", text)
        self.assertIn("dependencies: none", text)


class MachinePolicyMirrorTests(unittest.TestCase):
    """`config.yaml` calls itself a mirror of `config.json`; hold it to that."""

    def setUp(self) -> None:
        self.policy = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))

    def test_gate_policy_mirrors_the_machine_policy(self) -> None:
        self.assertEqual(_mirrored_gate_policy(), self.policy["gate_policy"])

    def test_branch_modifier_gates_mirror_the_machine_policy(self) -> None:
        expected = {
            branch: spec["gate"]
            for branch, spec in self.policy["branch_modifiers"].items()
        }

        self.assertEqual(_mirrored_branch_gates(), expected)

    def test_mirror_declares_itself_a_mirror(self) -> None:
        header = CONFIG_YAML.read_text(encoding="utf-8").splitlines()[0]

        self.assertIn("config.json", header)

    def test_every_path_the_mirror_names_exists(self) -> None:
        self.assertEqual(_unresolved(CONFIG_YAML.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
