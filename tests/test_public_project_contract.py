"""Regression checks for public security and contribution surfaces."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicProjectContractTests(unittest.TestCase):
    def test_security_model_covers_public_trust_boundaries(self) -> None:
        model = (ROOT / "docs/security-model.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

        for heading in (
            "## Command execution",
            "## Agent artifact publication",
            "## Loopback HTTP MCP transport",
            "## Proof integrity",
        ):
            self.assertIn(heading, model)
        for phrase in (
            "not a sandbox",
            "not cryptographic signing",
            "Residual user responsibility",
        ):
            self.assertIn(phrase, model)
        self.assertIn("docs/security-model.md", readme)
        self.assertIn("docs/security-model.md", security)

    def test_community_templates_request_actionable_evidence(self) -> None:
        bug = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8")
        feature = (ROOT / ".github/ISSUE_TEMPLATE/feature_request.yml").read_text(encoding="utf-8")
        pull_request = (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")

        for field in ("name:", "description:", "title:", "labels:", "body:"):
            self.assertIn(field, bug)
            self.assertIn(field, feature)
        for phrase in (
            "agentflow --version",
            "Proof/schema versions",
            "Reproduction",
            "Platform",
        ):
            self.assertIn(phrase, bug)
        for phrase in ("Use case", "Compatibility impact"):
            self.assertIn(phrase, feature)
        for phrase in ("CONTRIBUTING.md", "Agentflow task loop", "Validation"):
            self.assertIn(phrase, pull_request)

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
                self.assertTrue(
                    (document.parent / target).exists(),
                    f"{relative_path} links to missing local target {target}",
                )


if __name__ == "__main__":
    unittest.main()
