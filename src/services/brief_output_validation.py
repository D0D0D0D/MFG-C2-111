"""Shared validation for reviewer and client-visible ECN brief claims."""

from __future__ import annotations

from typing import Any

from framework.errors import SecurityViolationError
from src.evidence_gate.strict import StrictCoveragePolicy, find_uncited_numeric_or_comparative_claims


_COVERAGE_POLICY = StrictCoveragePolicy(
    free_text_paths=(
        ("formatted_output", "risk_hypotheses"),
        ("formatted_output", "recommended_checks"),
    ),
    claim_text_keys=("claim_text", "claim", "check"),
)


def validate_brief_claim_coverage(formatted_output: dict[str, Any]) -> None:
    """Reject numeric or comparative brief claims not covered by evidence."""
    coverage_output = dict(formatted_output)
    for collection in ("risk_hypotheses", "recommended_checks"):
        coverage_output[collection] = [
            {key: value for key, value in claim.items() if key != "source_ids"}
            if isinstance(claim, dict)
            else claim
            for claim in formatted_output.get(collection, [])
        ]
    flags = find_uncited_numeric_or_comparative_claims(
        {"formatted_output": coverage_output},
        formatted_output.get("evidence", []),
        policy=_COVERAGE_POLICY,
    )
    if flags:
        raise SecurityViolationError("S-3 evidence gate: free-text comparative claim lacks evidence coverage")
