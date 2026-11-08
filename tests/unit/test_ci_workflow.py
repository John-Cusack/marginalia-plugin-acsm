"""The CI workflow is a machine-consumed contract: parse it, assert meaning.

Two drifts this catches that a green build would not: a workflow that runs a
test path which no longer exists (the job goes red on unrelated grounds, or
silently tests nothing if the path is a glob), and a runner that lacks the
binaries the integration tests need — in which case those tests *skip*, and the
job looks green while proving nothing. The second job therefore also has to
fail on a skip, which is asserted here rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PACKAGE_ROOT / ".github" / "workflows" / "ci.yml"

# Binaries the X11 integration tests require, and the Debian packages that
# provide them. Kept here (not in the workflow) so a new requirement shows up as
# a failing test rather than as a silently skipped job.
REQUIRED_PACKAGES = {
    "xvfb": "Xvfb",
    "x11-utils": "xmessage",
    "xdotool": "xdotool",
    "imagemagick": "import",
    "matchbox-window-manager": "matchbox-window-manager",
}


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW.is_file(), "CI is part of the deliverable, not an afterthought"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def triggers(workflow: dict):
    """``on:`` is YAML 1.1's boolean true; normalise before reading it."""
    return workflow.get("on", workflow.get(True))


def steps_of(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def all_run_commands(workflow: dict, job: str) -> str:
    return "\n".join(step.get("run", "") for step in steps_of(workflow, job))


def pytest_targets(workflow: dict, job: str) -> list[str]:
    targets: list[str] = []
    for step in steps_of(workflow, job):
        words = step.get("run", "").split()
        for word in words:
            if word.startswith("tests/"):
                targets.append(word)
    return targets


def test_workflow_has_the_two_jobs_that_carry_the_evidence(workflow: dict):
    assert set(workflow["jobs"]) == {"unit", "integration-x11"}
    assert "pull_request" in triggers(workflow), "the gate runs on pull requests"


def test_ci_runs_only_read_permissions(workflow: dict):
    # A test-and-build job needs no write scope; keeping it read-only is cheap
    # defence for a repo whose workflow runs arbitrary pull-request code.
    assert workflow["permissions"] == {"contents": "read"}


def test_the_unit_matrix_covers_every_supported_python(workflow: dict):
    versions = workflow["jobs"]["unit"]["strategy"]["matrix"]["python"]
    assert versions == ["3.11", "3.12", "3.13"]


@pytest.mark.parametrize("job", ["unit", "integration-x11"])
def test_every_pytest_target_exists(workflow: dict, job: str):
    targets = pytest_targets(workflow, job)
    assert targets, f"{job} must run tests"
    for target in targets:
        assert (PACKAGE_ROOT / target).exists(), f"{job} runs {target}, which does not exist"


def test_unit_job_proves_the_vendored_copy_against_the_sibling_pack(workflow: dict):
    commands = all_run_commands(workflow, "unit")
    # Cloning the sibling is how the protocol guard actually runs; without it
    # the guard skips and the drift check silently disappears.
    assert "git clone" in commands and "marginalia-plugin-kindle" in commands
    assert "test_vendored_protocol" in commands


def test_unit_job_builds_and_checks_the_artefact(workflow: dict):
    commands = all_run_commands(workflow, "unit")
    assert "twine check --strict" in commands
    assert "test_built_wheel" in commands


def test_x11_job_installs_every_binary_the_integration_tests_need(workflow: dict):
    commands = all_run_commands(workflow, "integration-x11")
    missing = {
        binary: package
        for package, binary in REQUIRED_PACKAGES.items()
        if package not in commands
    }
    assert not missing, f"the X11 job would skip tests needing: {missing}"


def test_x11_job_fails_when_the_plumbing_tests_skip(workflow: dict):
    commands = all_run_commands(workflow, "integration-x11")
    assert "SKIPPED" in commands, "a skipped plumbing test is not evidence"
    assert "exit 1" in commands
