from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from app.agent.schemas import MAX_MESSAGE, StrictInput

Page = Literal["home", "about", "workspace", "ticket", "sop-list", "sop", "incident", "handover"]
Section = Literal["tickets", "schedule", "knowledge", "activity", "outgoing", "evaluation", "ticket-summary",
                  "incidents", "analysis", "actions", "work"]
Hint = Literal["TICKET_LIST_READY", "TICKET_SELECTED", "TICKET_STATUS_CHANGED", "SOP_SEARCH_EMPTY",
               "ANALYSIS_COMPLETED", "INCOMING_OPENED", "HANDOVER_OPENED", "WORK_EDIT_STARTED", "HANDOVER_EDIT_STARTED"]


class PageContext(StrictInput):
    page_id: Page = "home"
    section: Section | None = None
    run_id: UUID | None = None
    ticket_id: UUID | None = None
    sop_id: UUID | None = None
    incident_id: UUID | None = None
    handover_revision: int | None = Field(default=None, ge=1, le=2**31 - 1)
    analysis_id: UUID | None = None
    search_state: Literal["loading", "ready", "empty", "error"] | None = None

    @model_validator(mode="after")
    def check_scope(self):
        if (self.run_id is not None) != (self.page_id not in {"home", "about"}):
            raise ValueError("Invalid practice scope")
        if ((self.ticket_id is not None) != (self.page_id in {"ticket", "incident"})
                or (self.sop_id is not None) != (self.page_id == "sop")
                or (self.incident_id is not None) != (self.page_id == "incident")
                or (self.handover_revision is not None) != (self.page_id == "handover")
                or self.analysis_id is not None and self.page_id != "ticket"):
            raise ValueError("Invalid page entity")
        return self


class ChatInput(StrictInput):
    conversation_id: UUID
    request_id: UUID
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE)]
    page_context: PageContext = Field(default_factory=PageContext)
    active_hint_id: Hint | None = None


class ChatDraft(StrictInput):
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3500)]
    citation_keys: Annotated[list[str], Field(max_length=5)]
