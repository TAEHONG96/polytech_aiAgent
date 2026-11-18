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
        if request.get("purpose") == "handover":
            from app.handover.analysis import fake_response

            return fake_response(request, contents, config)
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


def generator():
    provider = os.environ.get("AGENT_PROVIDER", "fake")
    if provider == "fake":
        return FakeGenerator()
    key = os.environ.get("GEMINI_API_KEY")
    if provider != "gemini" or not key:
        raise AnalysisFailure("AI_CONFIGURATION", "워커의 Gemini 공급자와 API 키 설정을 확인하세요.")
    return GeminiGenerator(key)
