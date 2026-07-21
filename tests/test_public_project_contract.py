"""Regression checks for public security and contribution surfaces."""

from pathlib import Path
import re
import unittest
from urllib.parse import urljoin


ROOT = Path(__file__).resolve().parents[1]


ISSUE_FORM_HEADER = re.compile(
    r"name: (?P<name>[^\n]+)\n"
    r"description: (?P<description>[^\n]+)\n"
    r'title: "(?P<title>[^"\n]*)"\n'
    r"labels: \[(?P<label>[A-Za-z0-9_.-]+)\]\n"
    r"body:\n"
)
ISSUE_FORM_ATTRIBUTES = {
    "markdown": {"value"},
    "textarea": {"label", "description", "placeholder", "value", "render"},
    "input": {"label", "description", "placeholder", "value"},
    "dropdown": {"label", "description", "options"},
}
ISSUE_FORM_PLAIN_STRING = re.compile(r"[A-Za-z][A-Za-z0-9 .,'?()/`_-]*")
YAML_RESERVED_PLAIN_STRINGS = frozenset(
    {"null", "true", "false", "yes", "no", "on", "off"}
)


def _parse_plain_string(value: str) -> str:
    """Accept the repository's deliberately narrow textual scalar subset."""
    if (
        ISSUE_FORM_PLAIN_STRING.fullmatch(value) is None
        or value.lower() in YAML_RESERVED_PLAIN_STRINGS
    ):
        raise ValueError("invalid plain scalar")
    return value


def _parse_issue_form(text: str) -> dict[str, object]:
    """Parse and validate the strict GitHub issue-form subset used here."""
    if not text.endswith("\n") or "\t" in text:
        raise ValueError("issue form must end with a newline and use spaces")
    lines = text.splitlines()
    if len(lines) < 6:
        raise ValueError("issue form is incomplete")
    header = ISSUE_FORM_HEADER.fullmatch("\n".join(lines[:5]) + "\n")
    if header is None:
        raise ValueError("issue form header is invalid")

    body: list[dict[str, object]] = []
    identifiers: set[str] = set()
    index = 5
    while index < len(lines):
        item_match = re.fullmatch(r"  - type: ([a-z]+)", lines[index])
        if item_match is None:
            raise ValueError(f"invalid body item at line {index + 1}")
        item_type = item_match.group(1)
        if item_type not in ISSUE_FORM_ATTRIBUTES:
            raise ValueError(f"unsupported issue-form item type: {item_type}")
        item: dict[str, object] = {"type": item_type}
        index += 1

        while index < len(lines) and not lines[index].startswith("  - type: "):
            id_match = re.fullmatch(r"    id: ([A-Za-z0-9_-]+)", lines[index])
            if id_match:
                if "id" in item:
                    raise ValueError("issue-form item has duplicate id keys")
                identifier = _parse_plain_string(id_match.group(1))
                if identifier in identifiers:
                    raise ValueError(f"duplicate issue-form id: {identifier}")
                identifiers.add(identifier)
                item["id"] = identifier
                index += 1
                continue

            if lines[index] == "    attributes:":
                if "attributes" in item:
                    raise ValueError("issue-form item has duplicate attributes")
                attributes: dict[str, object] = {}
                index += 1
                while index < len(lines) and lines[index].startswith("      "):
                    attribute_match = re.fullmatch(
                        r"      ([a-z]+):(.*)", lines[index]
                    )
                    if attribute_match is None:
                        raise ValueError(f"invalid attribute at line {index + 1}")
                    key, raw_value = attribute_match.groups()
                    if key in attributes:
                        raise ValueError(f"duplicate issue-form attribute: {key}")
                    if key not in ISSUE_FORM_ATTRIBUTES[item_type]:
                        raise ValueError(f"unsupported {item_type} attributes: {key}")
                    raw_value = raw_value.strip()
                    index += 1
                    if raw_value == "|":
                        value_lines: list[str] = []
                        while index < len(lines) and lines[index].startswith("        "):
                            value_lines.append(lines[index][8:])
                            index += 1
                        if not value_lines:
                            raise ValueError(f"empty block value for {key}")
                        attributes[key] = "\n".join(value_lines)
                    elif not raw_value and key == "options":
                        options: list[str] = []
                        while index < len(lines):
                            option_match = re.fullmatch(r"        - (.+)", lines[index])
                            if option_match is None:
                                break
                            options.append(_parse_plain_string(option_match.group(1)))
                            index += 1
                        attributes[key] = options
                    elif raw_value:
                        attributes[key] = _parse_plain_string(raw_value)
                    else:
                        raise ValueError(f"empty issue-form attribute: {key}")
                item["attributes"] = attributes
                continue

            if lines[index] == "    validations:":
                if "validations" in item:
                    raise ValueError("issue-form item has duplicate validations")
                index += 1
                if index >= len(lines):
                    raise ValueError("issue-form validations are empty")
                required_match = re.fullmatch(r"      required: (true|false)", lines[index])
                if required_match is None:
                    raise ValueError(f"invalid validation at line {index + 1}")
                item["validations"] = {"required": required_match.group(1) == "true"}
                index += 1
                continue

            raise ValueError(f"invalid issue-form structure at line {index + 1}")

        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            raise ValueError(f"{item_type} item is missing attributes")
        if item_type == "markdown":
            if set(item) - {"type", "attributes"} or not attributes.get("value"):
                raise ValueError("markdown items require only attributes.value")
        else:
            if not item.get("id") or not attributes.get("label"):
                raise ValueError(f"{item_type} items require id and attributes.label")
        if item_type == "dropdown":
            options = attributes.get("options")
            if not isinstance(options, list) or not options or len(options) != len(set(options)):
                raise ValueError("dropdown options must be non-empty and unique")
        body.append(item)

    return {
        "name": _parse_plain_string(header.group("name")),
        "description": _parse_plain_string(header.group("description")),
        "title": header.group("title"),
        "labels": [_parse_plain_string(header.group("label"))],
        "body": body,
    }


class PublicProjectContractTests(unittest.TestCase):
    def test_artifact_publication_assigns_gitignore_responsibility(self) -> None:
        model = (ROOT / "docs/security-model.md").read_text(encoding="utf-8").lower()

        self.assertIn("does not publish", model)
        self.assertIn("keep `/.agent/` ignored", model)

    def test_mcp_security_docs_disclose_replay_execution(self) -> None:
        model = (ROOT / "docs/security-model.md").read_text(encoding="utf-8").lower()
        mcp = (ROOT / "docs/mcp.md").read_text(encoding="utf-8").lower()

        for document in (model, mcp):
            self.assertIn("replay", document)
            self.assertIn("attested", document)
            self.assertIn("selected root", document)

    def test_security_policy_has_private_fallback(self) -> None:
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

        self.assertIn("mailto:krstruzz@gmail.com", security)
        self.assertNotIn("through their GitHub profile", security)

    def test_security_model_covers_public_trust_boundaries(self) -> None:
        model = (ROOT / "docs/security-model.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

        boundaries = (
            "## Command execution",
            "## Agent artifact publication",
            "## Loopback HTTP MCP transport",
            "## Proof integrity",
        )
        for heading in boundaries:
            start = model.index(heading)
            end = model.find("\n## ", start + len(heading))
            section = model[start : end if end != -1 else None]
            for subsection in (
                "### Guarantee",
                "### Non-guarantee",
                "### Threat assumptions",
                "### Residual user responsibility",
            ):
                self.assertIn(subsection, section, f"{heading} is missing {subsection}")
        for phrase in (
            "not a sandbox",
            "not cryptographic signing",
        ):
            self.assertIn(phrase, model)
        self.assertIn("docs/security-model.md", readme)
        self.assertIn("docs/security-model.md", security)

    def test_community_templates_request_actionable_evidence(self) -> None:
        bug_text = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(
            encoding="utf-8"
        )
        feature_text = (ROOT / ".github/ISSUE_TEMPLATE/feature_request.yml").read_text(
            encoding="utf-8"
        )
        pull_request = (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")

        bug = _parse_issue_form(bug_text)
        feature = _parse_issue_form(feature_text)
        self.assertEqual(bug["labels"], ["bug"])
        self.assertEqual(feature["labels"], ["enhancement"])
        bug_ids = {item.get("id") for item in bug["body"] if isinstance(item, dict)}
        feature_ids = {
            item.get("id") for item in feature["body"] if isinstance(item, dict)
        }
        self.assertTrue({"summary", "reproduction", "version", "platform"} <= bug_ids)
        self.assertTrue({"use-case", "proposal", "compatibility"} <= feature_ids)
        with self.assertRaisesRegex(ValueError, "duplicate issue-form id"):
            _parse_issue_form(bug_text.replace("id: reproduction", "id: summary"))
        with self.assertRaisesRegex(ValueError, "invalid issue-form structure"):
            _parse_issue_form(bug_text.replace("    attributes:", "   attributes:", 1))
        for malformed_scalar in (
            bug_text.replace("name: Bug report", "name: [Bug report]"),
            bug_text.replace(
                "description: Report reproducible Agentflow behavior that differs from the documented contract.",
                "description: Steps: reproduce",
            ),
            *(bug_text.replace("name: Bug report", f"name: {value}") for value in (
                ".nan",
                ".inf",
                "0x10",
                "0o10",
                "01",
                "true",
                "false",
                "null",
                "yes",
                "no",
                "on",
                "off",
            )),
            bug_text.replace("labels: [bug]", "labels: [true]"),
            bug_text.replace("id: summary", "id: null"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid plain scalar"):
                _parse_issue_form(malformed_scalar)
        with self.assertRaisesRegex(ValueError, "unsupported dropdown attributes"):
            _parse_issue_form(
                bug_text.replace("      options:\n", "      multiple: true\n      options:\n")
            )
        for phrase in (
            "agentflow --version",
            "Proof/schema versions",
            "Reproduction",
            "Platform",
        ):
            self.assertIn(phrase, bug_text)
        for phrase in ("Use case", "Compatibility impact"):
            self.assertIn(phrase, feature_text)
        for phrase in ("CONTRIBUTING.md", "Agentflow task loop", "Validation"):
            self.assertIn(phrase, pull_request)

    def test_issue_template_config_routes_security_reports_privately(self) -> None:
        config = (ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text(encoding="utf-8")

        self.assertIn("blank_issues_enabled: false", config)
        self.assertIn(
            "https://github.com/kstruzzieri/agentflow/security/advisories/new", config
        )
        self.assertIn("SECURITY.md", config)

    def test_contribution_documents_link_required_public_policy(self) -> None:
        conduct = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

        self.assertIn("Contributor Covenant", conduct)
        self.assertIn("version 2.1", conduct)
        self.assertNotIn("[INSERT CONTACT METHOD]", conduct)
        self.assertIn("mailto:krstruzz@gmail.com", conduct)
        for target in (
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "docs/agent-artifacts.md",
            "docs/agent-workflow.md",
        ):
            self.assertIn(target, contributing)

    def test_pull_request_template_links_resolve_from_pr_body(self) -> None:
        template = (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")
        targets = re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", template)
        expected = {
            "../blob/main/docs/agent-artifacts.md": "docs/agent-artifacts.md",
            "../blob/main/CONTRIBUTING.md": "CONTRIBUTING.md",
            "../blob/main/SECURITY.md": "SECURITY.md",
        }

        self.assertEqual(set(targets), set(expected))
        base = "https://github.example/owner/repo/pull/123"
        for target, repository_path in expected.items():
            self.assertEqual(
                urljoin(base, target),
                f"https://github.example/owner/repo/blob/main/{repository_path}",
            )

    def test_local_markdown_links_resolve(self) -> None:
        documents = (
            "SECURITY.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "docs/security-model.md",
            ".github/pull_request_template.md",
        )
        for relative_path in documents:
            document = ROOT / relative_path
            for target in re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                target = target.strip("<>").split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                resolved = document.parent / target
                if relative_path == ".github/pull_request_template.md" and target.startswith(
                    "../blob/main/"
                ):
                    resolved = ROOT / target.removeprefix("../blob/main/")
                self.assertTrue(
                    resolved.exists(),
                    f"{relative_path} links to missing local target {target}",
                )


if __name__ == "__main__":
    unittest.main()
