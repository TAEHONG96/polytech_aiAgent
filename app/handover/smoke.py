"""Run a single handover analysis with synthetic data and report the observed usage."""

import asyncio
import json
import os
from dataclasses import asdict
from uuid import UUID

from app.agent.analysis import Usage
from app.agent.provider import AnalysisFailure, GeminiGenerator
from app.handover.analysis import draft_handover

SHIFT_ID = UUID(int=1)
TICKET_ID = str(UUID(int=2))
ITEM = {
    "id": TICKET_ID, "number": "INC-900001", "version": 1, "title": "합성 저장소 지연",
    "service": "synthetic-storage", "severity": "P1", "status": "in_progress",
    "impact": "격리된 테스트 데이터의 응답이 느립니다.", "description": "가상 스토리지 읽기 지연이 관측되었습니다.",
    "resolution": None, "next_action": "다음 교대에서 가상 읽기 지연 수치를 재확인합니다.",
    "warnings": "합성 테스트 볼륨에서만 관측했습니다.", "approval_required": False, "recheck_at": None,
    "work": [{"id": str(UUID(int=3)), "title": "가상 지연 지표 비교", "status": "done", "note": "비교를 기록했습니다."}],
    "gaps": [{"id": TICKET_ID + ":owner", "question": "후속 조치 담당자를 확인해 주세요."}],
    "history": [{"id": str(UUID(int=4)), "version": 1, "type": "created",
                 "created_at": "2026-11-18T00:00:00Z", "details": {}}],
}


class SmokeGenerator(GeminiGenerator):
    def __init__(self, key: str):
        super().__init__(key)
        self.steps = []

    async def generate(self, contents, config):
        entry = {"phase": "structured_output" if config.response_mime_type == "application/json" else "tool_selection"}
        self.steps.append(entry)
        try:
            response = await super().generate(contents, config)
        except AnalysisFailure as exc:
            entry.update(error=exc.code, http_status=getattr(exc.__cause__, "code", None))
            raise
        entry["status"] = "returned"
        return response


async def run() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is unavailable in this worker.")
        return 1
    model = SmokeGenerator(os.environ["GEMINI_API_KEY"])
    usage = Usage()
    traces = []
    result = None
    error = None
    upstream = None
    try:
        result = await draft_handover(shift_id=SHIFT_ID, items=[ITEM], user_id=UUID(int=5), model=model,
                                      usage=usage, seconds=90, trace=lambda trace: traces.append(asdict(trace)))
    except AnalysisFailure as exc:
        error = exc.code
        if cause := exc.__cause__:
            upstream = {"type": type(cause).__name__, "status": getattr(cause, "code", None)}
    finally:
        await model.close()
    print(json.dumps({"model": model.model, "usage": asdict(usage), "tools": traces, "steps": model.steps,
                      "highlights": len(result["highlights"]) if result else None,
                      "grounded_followups": len(result["followups"]) if result else None,
                      "error": error, "upstream": upstream}))
    return int(not result or not result["highlights"] or not result["followups"] or not traces)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
