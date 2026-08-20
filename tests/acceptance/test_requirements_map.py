"""Checks the requirements map against the suite that actually exists.

A traceability matrix maintained by hand is worth nothing after the second
refactor: tests get renamed, files get split, and the document goes on
asserting coverage that evaporated. So the map is executable. Every test it
points at has to collect, and a requirement claiming coverage has to point at
something.

This is what makes the map an artifact rather than a claim — and it is the
honest version of the Build Book's "acceptance tests" item, which asks for
traceability from spec to proof rather than for a particular prose style.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from tests.acceptance.requirements import COVERED, NONE, PARTIAL, REQUIREMENTS

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def collected_node_ids():
    """Every test the suite collects, as node ids.

    Collected by running pytest rather than by walking files with a regex: the
    question is what actually *runs*, and a file can contain a test that does
    not collect.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
    )
    ids = [
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if "::" in line and not line.startswith(("=", "ERROR", "FAILED"))
    ]
    if not ids:
        pytest.skip(f"Could not collect the suite: {result.stdout[-500:]}")
    return ids


class TestTheMapIsHonest:
    def test_every_referenced_test_exists(self, collected_node_ids):
        """The whole point. A renamed test breaks this rather than leaving a
        document that quietly lies about what is proven."""
        missing = []

        for requirement in REQUIREMENTS:
            for target in requirement.tests:
                normalised = target.replace("\\", "/")
                if not any(node.startswith(normalised) for node in collected_node_ids):
                    missing.append(f"{requirement.id} -> {target}")

        assert not missing, (
            "The requirements map points at tests that no longer exist:\n  "
            + "\n  ".join(missing)
            + "\nUpdate tests/acceptance/requirements.py to match reality."
        )

    def test_a_covered_requirement_names_its_proof(self):
        """`covered` with nothing behind it is the failure mode this document
        exists to prevent, so it cannot be expressed."""
        empty = [r.id for r in REQUIREMENTS if r.status == COVERED and not r.tests]

        assert not empty, f"Marked covered but naming no tests: {empty}"

    def test_an_uncovered_requirement_says_why(self):
        """A gap without a reason is indistinguishable from an oversight, and
        the reason is the part somebody reads."""
        unexplained = [
            r.id for r in REQUIREMENTS
            if r.status in (NONE, PARTIAL) and not r.note
        ]

        assert not unexplained, f"Gap with no explanation: {unexplained}"

    def test_statuses_are_known_values(self):
        allowed = {COVERED, PARTIAL, NONE}
        wrong = [r.id for r in REQUIREMENTS if r.status not in allowed]

        assert not wrong, f"Unknown status on: {wrong}"

    def test_requirement_ids_are_unique(self):
        ids = [r.id for r in REQUIREMENTS]

        assert len(ids) == len(set(ids)), "Duplicate requirement ids"

    def test_every_line_reference_is_inside_the_spec(self):
        """A line number pointing past the end of the Build Book is a citation
        nobody can check."""
        spec = REPO_ROOT / "build-book.txt"
        if not spec.exists():
            pytest.skip("build-book.txt not present")

        total = len(spec.read_text(encoding="utf-8", errors="replace").splitlines())
        out_of_range = [r.id for r in REQUIREMENTS if not (1 <= r.line <= total)]

        assert not out_of_range, (
            f"Line references outside build-book.txt (1-{total}): {out_of_range}"
        )
