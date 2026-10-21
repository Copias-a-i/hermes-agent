"""Install/update E2E orchestration admits release tags before fan-out."""
from pathlib import Path

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "install-e2e.yml"


def _workflow() -> dict:
    return YAML(typ="base").load(WORKFLOW.read_text(encoding="utf-8"))


def test_release_picker_checkout_fetches_full_tag_history_for_tree_inspection():
    """Tag selection calls git ls-tree, so tag refs and their target trees must exist."""
    picker = _workflow()["jobs"]["pick-releases"]
    checkout = next(step for step in picker["steps"] if "actions/checkout@" in step.get("uses", ""))

    assert checkout["with"]["fetch-tags"] == "true"
    assert checkout["with"]["fetch-depth"] == "0"


def test_result_chart_checks_picker_outcome_before_parsing_its_output():
    """A failed picker must report its own failure, never hand empty text to JSON.parse."""
    report = _workflow()["jobs"]["report"]
    step = next(step for step in report["steps"] if "generate-e2e-matrix.mjs" in step.get("run", ""))

    assert step["env"]["PICK_RELEASES_RESULT"] == "${{ needs.pick-releases.result }}"
    assert step["env"]["PICK_RELEASES_OUTCOME"] == "${{ needs.pick-releases.outputs.outcome }}"
    assert step["env"]["RELEASE_TAGS"] == "${{ needs.pick-releases.outputs.tags }}"
    assert '"$PICK_RELEASES_OUTCOME" = "no_candidates"' in step["run"]
    assert "No eligible releases; install E2E not run." in step["run"]
    assert "This is not a passing install-coverage result." in step["run"]
    assert '"$PICK_RELEASES_RESULT" != "success"' in step["run"]
    assert 'jq -e' in step["run"]
    assert '"$RELEASE_TAGS"' in step["run"]


def test_picker_workflow_treats_only_exit_three_as_no_candidates():
    """Broken picker invocations must fail the job rather than skip coverage."""
    picker = _workflow()["jobs"]["pick-releases"]
    picker_step = next(step for step in picker["steps"] if step.get("id") == "pick")

    assert 'if [ "$picker_status" -eq 3 ]; then' in picker_step["run"]
    assert 'echo "outcome=no_candidates" >> "$GITHUB_OUTPUT"' in picker_step["run"]
    assert 'exit "$picker_status"' in picker_step["run"]


def test_no_candidates_is_a_successful_typed_picker_outcome_that_skips_matrix_fan_out():
    """Bootstrap repositories report unavailable coverage without faking a test pass."""
    workflow = _workflow()
    picker = workflow["jobs"]["pick-releases"]
    picker_step = next(step for step in picker["steps"] if step.get("id") == "pick")
    matrix = workflow["jobs"]["generate-matrix"]

    assert picker["outputs"]["outcome"] == "${{ steps.pick.outputs.outcome }}"
    assert 'echo "outcome=no_candidates" >> "$GITHUB_OUTPUT"' in picker_step["run"]
    assert 'echo "tags=[]" >> "$GITHUB_OUTPUT"' in picker_step["run"]
    assert 'exit 0' in picker_step["run"]
    assert matrix["if"] == "needs.pick-releases.outputs.outcome == 'candidates'"
