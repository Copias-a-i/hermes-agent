"""Source locks for measured standard-hosted-runner CI capacity."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
JS_WORKFLOW = ROOT / ".github/workflows/js-tests.yml"
WORKSPACE_RUNNER = ROOT / ".github/scripts/run-workspace-checks.mjs"
TESTS_WORKFLOW = ROOT / ".github/workflows/tests.yml"


def test_js_checks_use_explicit_two_check_standard_runner_budget():
    workflow = JS_WORKFLOW.read_text(encoding="utf-8")
    runner = WORKSPACE_RUNNER.read_text(encoding="utf-8")

    assert "runs-on: ubuntu-latest" in workflow
    assert "run: node .github/scripts/run-workspace-checks.mjs --concurrency 2" in workflow
    assert "availableParallelism" not in runner
    assert "Math.min(units.length, 2)" in runner


def test_linux_e2e_reserves_capacity_for_heartbeat_processes():
    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert 'HERMES_TEST_WORKERS: "2"' in workflow
    assert 'HERMES_TEST_FILE_TIMEOUT: "900"' in workflow