import os
import re
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_MESSAGE = 2000
MAX_TOOL_CALLS = 8
MAX_TOOL_ROUNDS = 3
MAX_MODEL_REQUESTS = 8
RUN_SECONDS = 90
QUEUE_SECONDS = 120
REQUEST_SECONDS = 25
FINAL_RESERVE_SECONDS = 26
TOOL_SECONDS = 15

SECRET_VALUE = re.compile(
    r"(?i)(\b(?:authorization\s*:\s*bearer|bearer|api[_ -]?key|access[_ -]?token|"
    r"password|secret|비밀번호|인증\s*토큰)\s*(?:[:=]\s*|\s+))[^\s,\"';]+"
)
PRIVATE_KEY = re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----", re.DOTALL)


def clean_text(text: str, limit: int) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    text = PRIVATE_KEY.sub("[REDACTED]", text)
    return SECRET_VALUE.sub(r"\1[REDACTED]", text)[:limit]


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalyzeInput(StrictInput):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE)]
    ticket_id: UUID | None = None


class Evidence(StrictInput):
    key: str
    kind: str
    source_id: UUID
    title: str
    content: str
    source_url: str
    score: float | None = None
    chunk_index: int | None = None
    page_number: int | None = None
    section: str | None = None
    ticket_version: int | None = None


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
References = Annotated[list[str], Field(max_length=4)]


class Statement(StrictInput):
    text: ShortText
    citation_keys: References


class AnalysisDraft(StrictInput):
    summary: ShortText
    confirmed_facts: Annotated[list[Statement], Field(max_length=8)]
    hypotheses: Annotated[list[Statement], Field(max_length=6)]
    diagnostic_steps: Annotated[list[Statement], Field(max_length=8)]
    additional_information: Annotated[list[ShortText], Field(max_length=6)]
    limitations: Annotated[list[ShortText], Field(max_length=6)]


class GroundingError(ValueError):
    pass


def validate_draft(raw: str, sources: dict[str, Evidence]) -> dict:
    draft = AnalysisDraft.model_validate_json(raw)
    data = draft.model_dump(mode="json")
    cited_keys = set()
    fact_kinds = {"TICKET_DETAILS", "TICKET_HISTORY", "OPEN_TICKET"}
    for field in ("confirmed_facts", "hypotheses", "diagnostic_steps"):
        for item in data[field]:
            keys = set(item["citation_keys"])
            if not keys <= sources.keys():
                raise GroundingError("Unknown evidence key")
            if field == "confirmed_facts" and (not keys or any(sources[key].kind not in fact_kinds for key in keys)):
                raise GroundingError("Confirmed facts require direct ticket evidence")
            cited_keys.update(keys)
            item["basis_status"] = "supported" if keys else "insufficient"
            item["text"] = clean_text(item["text"], 500)
    for field in ("additional_information", "limitations"):
        data[field] = [clean_text(item, 500) for item in data[field]]
    data["summary"] = clean_text(data["summary"], 500)
    if not cited_keys:
        data["limitations"].append("확인된 근거가 부족합니다. 추가 정보로 가설을 검증해야 합니다.")
    data["evidence"] = [item.model_dump(mode="json") for key, item in sources.items() if key in cited_keys]
    return data
