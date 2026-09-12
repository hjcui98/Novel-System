"""Explicit positive assessments for fake Editor orchestration fixtures only."""

import json
import re


def complete_fake_review(response: str, prompt: str) -> str:
    try:
        output = json.loads(response)
    except (ValueError, TypeError):
        return response
    if not isinstance(output, dict) or "verdict" not in output:
        return response
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", prompt):
        try:
            payload, _ = decoder.raw_decode(prompt[match.start() :])
        except ValueError:
            continue
        if not isinstance(payload, dict) or "plan_checklist" not in payload:
            continue
        text = payload["draft_text"]
        output["plan_assessments"] = [
            {
                "criterion_id": item["criterion_id"],
                "satisfied": True,
                "rationale": "Positive fixture judgment for control-flow testing.",
                "evidence_quotes": [text],
            }
            for item in payload["plan_checklist"]
        ]
        output["memory_gap_assessments"] = [
            {
                "gap": gap,
                "disposition": "avoided",
                "required_for_plan": False,
                "draft_evidence_quotes": [text],
                "rationale": "Fixture avoids the missing detail.",
                "evidence_quotes": [],
            }
            for gap in payload["unresolved_memory_gaps"]
        ]
        return json.dumps(output)
    return response
