"""Turn decision-model probabilities into one of: auto, require_approval, block_and_replan, quarantine_content."""
from __future__ import annotations


def decide(probs: dict[str, float], policy: dict, exempt: frozenset[str] = frozenset()) -> str:
    """The most restrictive gate that fires wins. Exempt questions are ignored, e.g. credential_use
    for a platform-managed placeholder credential the sandbox never holds."""
    hits = {g["then"] for g in policy["gates"]
            if any(c["q"] not in exempt and probs.get(c["q"], 0.0) >= c["gte"] for c in g["any"])}
    return next((d for d in policy["decision_order"] if d in hits), "auto")
