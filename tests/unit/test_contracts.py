import pytest
from pydantic import ValidationError

from crew.agents.contracts import DirectorPlan, EvaluationReport, ReviewReport


def test_director_plan_requires_tasks_with_acceptance_criteria():
    with pytest.raises(ValidationError):
        DirectorPlan.model_validate(
            {"understanding": "something useful", "approach": "a sensible plan", "tasks": []}
        )
    with pytest.raises(ValidationError):
        DirectorPlan.model_validate(
            {
                "understanding": "something useful",
                "approach": "a sensible plan",
                "tasks": [
                    {
                        "id": "a",
                        "title": "Do A",
                        "description": "describe A properly",
                        "acceptance_criteria": [],
                    }
                ],
            }
        )


def test_review_blocking_findings_and_location():
    review = ReviewReport.model_validate(
        {
            "assessment": "one problem",
            "findings": [
                {
                    "severity": "HIGH",
                    "category": "correctness",
                    "title": "Off by one",
                    "file": "app.py",
                    "line_start": 3,
                    "explanation": "loop bound wrong",
                    "blocking": True,
                },
                {
                    "severity": "LOW",
                    "category": "maintainability",
                    "title": "Naming",
                    "explanation": "unclear name",
                    "blocking": False,
                },
            ],
        }
    )
    assert [f.title for f in review.blocking_findings] == ["Off by one"]
    assert review.findings[0].location == "app.py:3"


def test_evaluation_rejects_unknown_recommendation():
    with pytest.raises(ValidationError):
        EvaluationReport.model_validate(
            {
                "recommendation": "MAYBE",
                "requirements": [{"requirement": "x", "status": "met"}],
                "explanation": "because",
            }
        )
