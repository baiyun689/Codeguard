from copy import deepcopy

import pytest

from evals.adjudication import prepare, score


def archive():
    return {"runs": 1, "cases": [{"case_id": "v", "gold_issue_ids": ["E1", "E2"],
            "reported_issues": [{"message": "first"}, {"message": "duplicate"}, {"message": "wrong"}]},
            {"case_id": "clean", "gold_issue_ids": [], "reported_issues": [{"message": "false alarm"}]}]}


def reviewed(raw):
    form = prepare(raw)
    form.update(reviewer="local-reviewer", gold_labels_checked=True)
    for case in form["cases"]:
        for report in case["reports"]:
            report.update(verdict="false_positive", rationale="Checked trigger and counterexample", evidence=["oracle.log"])
    reports = form["cases"][0]["reports"]
    reports[0].update(verdict="correct", expected_id="E1")
    reports[1].update(verdict="duplicate", duplicate_of=0)
    return form


def test_pending_is_not_automatically_scored_as_rule_hit():
    raw = archive()
    with pytest.raises(ValueError):
        score(raw, prepare(raw))


def test_distinguishes_duplicate_false_report_and_known_recall():
    raw = archive()
    result = score(raw, reviewed(raw))
    assert result["counts"]["tp"] == 1
    assert result["counts"]["fn"] == 1
    assert result["counts"]["duplicate_reports"] == 1
    assert result["counts"]["false_reports"] == 2
    assert result["counts"]["clean_cases_with_false_reports"] == 1
    assert result["known_defect_recall"] == 0.5
    assert result["unique_valid_report_precision"] == 0.25


@pytest.mark.parametrize("mutation", ["archive", "gold", "report", "pending", "duplicate", "missing"])
def test_rejects_tampering_and_incomplete_review(mutation):
    raw = archive()
    form = reviewed(raw)
    if mutation == "archive":
        raw["model"] = "changed"
    elif mutation == "gold":
        form["cases"][0]["gold_issue_ids"].pop()
    elif mutation == "report":
        # Clone to avoid changing the archive held by prepare.
        form = deepcopy(form)
        form["cases"][0]["reports"][0]["report"]["message"] = "edited"
    elif mutation == "pending":
        form["cases"][0]["reports"][2]["verdict"] = "pending"
    elif mutation == "duplicate":
        form["cases"][0]["reports"][1]["duplicate_of"] = 1
    else:
        form["cases"].pop()
    with pytest.raises(ValueError):
        score(raw, form)


def test_clean_contradiction_requires_dataset_revision():
    raw = archive()
    form = reviewed(raw)
    form["cases"][1]["reports"][0]["verdict"] = "novel_valid"
    with pytest.raises(ValueError, match="Clean label contradicted"):
        score(raw, form)


def test_repeated_archive_not_silently_last_run_only():
    raw = archive()
    raw["runs"] = 3
    with pytest.raises(ValueError):
        prepare(raw)
