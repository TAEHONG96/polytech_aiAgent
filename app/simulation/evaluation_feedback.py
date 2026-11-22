from typing import Annotated

from google.genai import types
from pydantic import Field, ValidationError

from app.agent.analysis import user_content
from app.agent.provider import AnalysisFailure, reply
from app.agent.schemas import StrictInput
from app.simulation.evaluation_rules import RULES

Keys = Annotated[list[str], Field(max_length=3)]


class Selection(StrictInput):
    focus: str
    strengths: Keys
    improvements: Keys
    recommendations: Keys


def valid_choices(rules: list[dict]) -> dict[str, list[str]]:
    strengths = [rule["rule_code"] for rule in rules if rule["result"] == "PASS"]
    improvements = [rule["rule_code"] for rule in rules if rule["result"] in ("NOT_MET", "NOT_OBSERVED")]
    return {"focus": strengths + improvements, "strengths": strengths,
            "improvements": improvements, "recommendations": improvements}


def render(selection: Selection, rules: list[dict]) -> dict:
    valid = valid_choices(rules)
    verdicts = {rule["rule_code"]: rule["result"] for rule in rules}
    for key in ("strengths", "improvements", "recommendations"):
        chosen = getattr(selection, key)
        if len(chosen) != len(set(chosen)) or (bool(chosen) != bool(valid[key])) or not set(chosen) <= set(valid[key]):
            raise ValueError("Unsupported evaluation feedback keys")
    if selection.focus not in valid["focus"]:
        raise ValueError("Unsupported evaluation feedback focus")

    def sentence(key):
        label, _ = RULES[key]
        return {
            "PASS": f"{label} 기준을 충족한 근거가 기록됐습니다.",
            "NOT_MET": f"{label} 기준에 필요한 근거가 부족합니다.",
            "NOT_OBSERVED": f"{label}에 해당하는 검증 기록을 확인하지 못했습니다.",
        }[verdicts[key]]

    return {"summary": sentence(selection.focus), "strengths": [sentence(key) for key in selection.strengths],
            "improvements": [sentence(key) for key in selection.improvements],
            "recommendations": [RULES[key][1] for key in selection.recommendations]}


async def explain(model, ticket: dict, rules: list[dict], evidence: list[dict]) -> dict:
    # Send rules and fixed evidence identifiers/classification. Free notes, private data, and draft prose stay local.
    request = {
        "purpose": "evaluation_feedback", "final_status": ticket["status"], "warnings": ticket["warnings"],
        "rules": rules, "evidence": evidence, "choices": valid_choices(rules),
    }
    config = types.GenerateContentConfig(
        system_instruction=("한국어 교육 피드백의 초점과 항목 순서를 선택합니다. 각 배열에는 choices에 주어진 키만 넣고, "
                            "가능한 값이 있으면 최소 1개, 최대 3개를 선택하세요. 새로운 사실 또는 자유 문장을 넣지 마세요."),
        response_mime_type="application/json", response_json_schema=Selection.model_json_schema(),
        max_output_tokens=1024,
    )
    response = await model.generate([user_content(request)], config)
    try:
        return render(Selection.model_validate_json(response.text or ""), rules)
    except (ValidationError, ValueError, KeyError) as exc:
        raise AnalysisFailure("EVALUATION_INVALID_FEEDBACK", "검증 가능한 피드백 형식을 받지 못했습니다.", retryable=True) from exc


def fake_response(request):
    choices = request["choices"]
    selected = Selection(focus=choices["focus"][0], strengths=choices["strengths"][:3],
                         improvements=choices["improvements"][:3], recommendations=choices["recommendations"][:3])
    return reply(types.Part(text=selected.model_dump_json()))
