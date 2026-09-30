"""Static regression gate for non-negotiable KCP authority and safety rules."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "src" / "aos" / "knowledge"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"KCP_REGRESSION: {message}")


def main() -> int:
    ledger = (KNOWLEDGE / "ledger.py").read_text("utf-8")
    model = (KNOWLEDGE / "model.py").read_text("utf-8")
    mirror = (KNOWLEDGE / "mirror.py").read_text("utf-8")
    index = (KNOWLEDGE / "index.py").read_text("utf-8")
    context = (KNOWLEDGE / "context.py").read_text("utf-8")
    accepted_work = (KNOWLEDGE / "accepted_work.py").read_text("utf-8")
    hooks = (KNOWLEDGE / "hooks.py").read_text("utf-8")
    combined = "\n".join(path.read_text("utf-8") for path in KNOWLEDGE.glob("*.py"))

    require('open("ab")' in ledger, "ledger append mode was removed")
    require("exclusive_file_lock" in ledger and "os.fsync" in ledger, "atomic lock-safe append guarantee missing")
    require("previous_hash" in ledger and "content_hash" in ledger, "ledger hash chain missing")
    require("advisory_only" in mirror and "SECOND_BRAIN_MIRROR" in mirror, "second brain advisory boundary missing")
    require("DECISION_ACCEPTED" not in mirror, "mirror can create accepted decisions")
    require("operational_truth_must_be_refreshed" in index, "historical operational truth guard missing")
    require("FRESH_CURRENT_TRUTH_REQUIRED" in context, "fresh current-truth preflight guard missing")
    require("IMPLEMENTATION_RECEIPT" in accepted_work and "VERIFICATION_RECEIPT" in accepted_work,
            "accepted-work two-receipt coverage guard missing")
    require("HANDOFF" not in accepted_work and "CURRENT_TRUTH_OBSERVATION" not in accepted_work,
            "non-acceptance events entered accepted-work coverage")
    require("disable_kcp" not in combined.lower() and "bypass_kcp" not in combined.lower(),
            "permanent KCP disable escape hatch detected")
    require("os.environ[" not in hooks and "os.environ.setdefault" not in hooks,
            "process-global knowledge-home mutation detected")
    require('"NO_GO"' in model and '"DISABLED"' in model, "safety constants missing")
    require('production"] != "NO_GO"' in model, "production fail-closed validation missing")
    require('paid_fallback"] != "DISABLED"' in model, "paid fallback fail-closed validation missing")
    require("PRODUCTION = \"GO\"" not in combined, "KCP enables production")
    require("PAID_FALLBACK = \"ENABLED\"" not in combined, "KCP enables paid fallback")
    print("KCP_REGRESSION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
