import json
from pathlib import Path

from aos.validate import validate_file


ROOT = Path(__file__).resolve().parents[1]


def test_disabled_jev_policy_is_valid_and_closed():
    result, code = validate_file(
        "decision_advisor_policy", ROOT / "descriptors" / "jev.decision-policy.json"
    )
    assert code == 0, [str(error) for error in result.errors]


def test_policy_schema_rejects_enablement_paid_fallback_and_production(tmp_path):
    original = json.loads((ROOT / "descriptors" / "jev.decision-policy.json").read_text())
    for field, value in (
        ("enabled", True), ("paid_budget_usd", 1.0),
        ("allow_paid_fallback", True), ("production", "GO"),
    ):
        candidate = dict(original)
        candidate[field] = value
        path = tmp_path / f"{field}.json"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        result, code = validate_file("decision_advisor_policy", path)
        assert code == 1
        assert not result.is_valid
