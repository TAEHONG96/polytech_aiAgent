import json
import os

import httpx2
from google import genai
from google.genai import errors, types

GENERATION_MODEL = "gemini-3.8-flash"


class AnalysisFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class GeminiGenerator:
    provider = "gemini"

    def __init__(self, key: str):
        self.model = os.environ.get("GEMINI_GENERATION_MODEL", GENERATION_MODEL)
        self.client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=25_000, retry_options=types.HttpRetryOptions(attempts=1)),
        )

    async def generate(self, contents: list[types.Content], config: types.GenerateContentConfig):
        if self.model == GENERATION_MODEL:
            config = config.model_copy(update={"thinking_config": types.ThinkingConfig(thinking_level="LOW")})
        try:
            return await self.client.aio.models.generate_content(model=self.model, contents=contents, config=config)
        except errors.APIError as exc:
            retry_after = None
            if response := getattr(exc, "response", None):
                try:
                    retry_after = max(0, float(response.headers.get("Retry-After", "")))
                except (TypeError, ValueError):
                    pass
            if exc.code == 429:
                raise AnalysisFailure("AI_RATE_LIMIT", "Gemini 호출 한도를 초과했습니다.", retryable=True,
                                      retry_after=retry_after) from exc
            if exc.code in (408, 500, 502, 503, 504):
                raise AnalysisFailure("AI_UNAVAILABLE", "Gemini API가 일시적으로 응답하지 않습니다.",
                                      retryable=True, retry_after=retry_after) from exc
            code = "AI_CONFIGURATION" if exc.code in (401, 403, 404) else "AI_REQUEST_REJECTED"
            raise AnalysisFailure(code, "Gemini 모델 접근과 요청 설정을 확인하세요.") from exc
        except (httpx2.TransportError, TimeoutError) as exc:
            raise AnalysisFailure("AI_UNAVAILABLE", "Gemini에 연결할 수 없습니다.", retryable=True) from exc

    async def close(self) -> None:
        await self.client.aio.aclose()


def reply(*parts: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)), finish_reason="STOP")]
    )


class FakeGenerator:
    provider = "fake"
    model = "fake-analysis-v1"

    async def generate(self, contents: list[types.Content], config: types.GenerateContentConfig):
        request = json.loads(contents[0].parts[0].text)
        if request.get("purpose") == "workflow_plan":
            return reply(types.Part(text=json.dumps({"steps": request["allowed_steps"]})))
        if request.get("purpose") == "companion_tutor":
            ticket = request["verified_ticket"]
            source = next(iter(request["evidence"]), None)
            subject = ticket["observed_error"] or ticket["title"]
            guides = {
                1: f"티켓의 {subject} 관측에서 문서 검색어를 좁히는 데 쓸 단서는 무엇일까요?",
                2: f"티켓의 장애 개요에서 {subject} 관련 관측을 찾아 승인 SOP 적용 조건과 함께 살펴보세요.",
                3: (f"{source['title']}의 적용 범위와 {subject} 관측이 일치하는 항목을 골라 기록해 보세요."
                    if source else f"{subject}의 관측값과 확인 시점을 기록해 다음 비교의 기준을 만들어 보세요."),
            }
            return reply(types.Part(text=json.dumps({"answer": guides[request["level"]],
                "citation_keys": [source["key"]] if source else []}, ensure_ascii=False)))
        if request.get("purpose") == "workflow_compare_sops":
            ticket, sources = request["verified_ticket"], request["evidence"]
            assessments = [{"key": source["key"], "reason": f"{ticket['title']}의 관측을 문서 발췌와 대조하는 합성 평가입니다.",
                            "match": ticket["observed_error"] or ticket["description"][:120],
                            "scope": (source["matched_excerpt"] or source["document_excerpt"])[:150],
                            "limitation": "실제 적용 여부는 문서의 전체 적용 조건과 추가 관측을 확인하세요."} for source in sources]
            return reply(types.Part(text=json.dumps({"assessments": assessments, "recommended_key": sources[0]["key"],
                "recommendation_reason": "이 합성 테스트에서는 첫 후보의 발췌문을 우선 확인합니다."}, ensure_ascii=False)))
        if request.get("purpose") == "workflow_explain_handover":
            context = request["verified"]
            return reply(types.Part(text=json.dumps({
                "purpose": f"발신 인계는 다음 Bot에게 업무를 전달하는 화면이에요. 현재 리비전은 {context['handover']['handover_revision']}입니다.",
                "review_fields": "화면에서 " + ", ".join(context["outgoing_fields"]) + " 항목을 확인하세요.",
                "previous_vs_current": "이전 Bot 인계는 근무 영역의 참고 기록이며, 발신 인계는 현재 교육생의 교대 기록입니다.",
                "next_shift": "다음 Bot은 인계 요약과 포함된 티켓별 다음 조치를 읽습니다.",
                "unresolved_work": "포함된 티켓의 상태와 기록된 다음 조치를 함께 확인하세요.",
            }, ensure_ascii=False)))
        if request.get("purpose") == "workflow_summary":
            ticket, sources = request["verified"], request["evidence"]
            answer = (f"**관측된 사실** · {ticket['title']}. {ticket['description']}\n\n"
                      f"**다음 확인 항목** · {ticket['next_action'] or '관측 로그와 승인 SOP를 비교해 주세요.'}\n\n"
                      "**아직 확인되지 않은 사항** · 장애 원인과 추가 점검 결과")
            return reply(types.Part(text=json.dumps({"answer": answer, "citation_keys": [sources[0]["key"]]}, ensure_ascii=False)))
        if request.get("purpose") == "companion_chat":
            from app.companion.service import fake_response

            return fake_response(request)
        if request.get("purpose") == "handover":
            from app.handover.analysis import fake_response

            return fake_response(request, contents, config)
        if request.get("purpose") == "evaluation_feedback":
            from app.simulation.evaluation_feedback import fake_response

            return fake_response(request)
        if config.response_mime_type == "application/json":
            sources = request["evidence"]
            current = next((item for item in sources if item["kind"] == "TICKET_DETAILS"), None)
            similar = next((item for item in sources if item["kind"] == "SIMILAR_TICKET"), None)
            sop = next((item for item in sources if item["kind"] == "SOP"), None)
            facts = [{"text": current["content"][:450], "citation_keys": [current["key"]]}] if current else []
            summary = "가상 분석입니다. 관찰된 증상과 참조 자료를 검토했습니다." if sources else "운영 장애 증상과 영향 범위를 알려 주세요."
            data = {
                "summary": summary,
                "confirmed_facts": facts,
                "hypotheses": [{"text": "참조한 과거 사례와 같은 장애 유형일 가능성이 있습니다.", "citation_keys": [similar["key"]]}] if similar else [],
                "diagnostic_steps": [{"text": "참조 SOP의 점검 항목과 실제 관측 기록을 비교하세요.", "citation_keys": [sop["key"]]}] if sop else [],
                "additional_information": ["발생 시각과 관찰한 진단 메시지를 확인해 주세요."],
                "limitations": ["합성 공급자의 시연 결과입니다."],
            }
            return reply(types.Part(text=json.dumps(data, ensure_ascii=False)))

        responses = [
            part.function_response for content in contents for part in (content.parts or [])
            if part.function_response is not None
        ]
        names = {response.name for response in responses}
        ticket = request.get("ticket_id")
        question = request["message"]
        if ticket and "get_ticket_details" not in names:
            name, args = "get_ticket_details", {"ticket_id": ticket}
        elif "get_open_tickets" not in names and any(word in question for word in ("진행 중인", "미해결", "열린 티켓")):
            name, args = "get_open_tickets", {"filters": {"limit": 5}}
        elif any(word in question.casefold() for word in ("장애", "부팅", "서버", "오류", "메모리", "raid", "네트워크", "post")):
            if "search_similar_tickets" not in names:
                name, args = "search_similar_tickets", {"query": question[:300], "limit": 3}
            elif "search_sop" not in names:
                source = next((item for item in responses if item.name == "search_similar_tickets"), None)
                matches = source.response.get("untrusted_data", []) if source else []
                refined = matches[0]["title"] if matches else question[:300]
                name, args = "search_sop", {"query": refined, "limit": 3}
            else:
                return reply(types.Part(text="Investigation complete."))
        else:
            return reply(types.Part(text="No relevant investigation."))
        return reply(types.Part(function_call=types.FunctionCall(name=name, args=args)))

    async def close(self) -> None:
        return None


def generator(*, default_provider: str = "fake"):
    provider = os.environ.get("AGENT_PROVIDER", default_provider)
    if provider == "fake":
        return FakeGenerator()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if provider != "gemini" or not key:
        raise AnalysisFailure("AI_CONFIGURATION", "워커의 Gemini 공급자와 API 키 설정을 확인하세요.")
    return GeminiGenerator(key)
