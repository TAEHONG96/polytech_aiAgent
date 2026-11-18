from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.agent.schemas import StrictInput
from app.tickets import InputTime, Note, Title, Version


class HandoverStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    CONFIRMED = "CONFIRMED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class Selection(StrictInput):
    include_ticket_ids: list[UUID] = Field(default_factory=list, max_length=100)
    exclude_ticket_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check_selection(self):
        combined = self.include_ticket_ids + self.exclude_ticket_ids
        if len(set(combined)) != len(combined):
            raise ValueError("각 티켓은 한 번만 포함하거나 제외할 수 있습니다.")
        return self


class HandoverCreate(Selection):
    outgoing_shift_id: UUID
    incoming_shift_id: UUID | None = None


class VersionInput(StrictInput):
    expected_version: Version


class GapResponse(StrictInput):
    gap_id: Annotated[str, Field(min_length=1, max_length=100)]
    answer: Note


class ItemNote(StrictInput):
    ticket_id: UUID
    summary: Note | None = None
    followup: Note | None = None
    warnings: Note | None = None


class ReportBody(StrictInput):
    summary: Note | None = None
    gap_responses: list[GapResponse] = Field(default_factory=list)
    item_notes: list[ItemNote] = Field(default_factory=list)


class HandoverEdit(VersionInput):
    body: ReportBody | None = None
    selection: Selection | None = None
    refresh_snapshot: bool = False
    status: Literal["DRAFT", "READY"] = "DRAFT"

    @model_validator(mode="after")
    def check_refresh(self):
        if (self.refresh_snapshot or self.selection is not None) and self.status == "READY":
            raise ValueError("자료를 갱신한 뒤 준비 완료를 선택하세요.")
        return self


class TicketOperations(VersionInput):
    shift_instance_id: UUID | None = None
    owner_id: UUID | None = None
    warnings: Note | None = None
    approval_required: bool | None = None


class WorkInput(VersionInput):
    title: Title
    status: Literal["todo", "in_progress", "blocked", "done"]
    owner_id: UUID | None = None
    due_at: InputTime | None = None
    note: Note | None = None
