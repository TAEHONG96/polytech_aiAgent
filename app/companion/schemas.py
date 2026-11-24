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
    action_view_id: UUID | None = None


ActionType = Literal["navigate_to_page", "open_sop", "highlight_element", "focus_input"]
RouteId = Literal["practice_home", "workspace", "ticket_list", "outgoing_handover", "sop_library"]
AnchorId = Literal["search-sop", "ticket-search", "sop-query", "outgoing-handover"]


class ActionDraft(StrictInput):
    type: ActionType
    route_id: RouteId | None = None
    anchor_id: AnchorId | None = None
    sop_key: Annotated[str, StringConstraints(pattern=r"^(?:K[0-9]{1,3}|PAGE_SOP)$")] | None = None

    @model_validator(mode="after")
    def check_arguments(self):
        fields = {"navigate_to_page": "route_id", "open_sop": "sop_key",
                  "highlight_element": "anchor_id", "focus_input": "anchor_id"}
        if {name for name in ("route_id", "anchor_id", "sop_key") if getattr(self, name) is not None} != {fields[self.type]}:
            raise ValueError("Invalid action arguments")
        return self


class ChatDraft(StrictInput):
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3500)]
    citation_keys: Annotated[list[str], Field(max_length=5)]
    action_proposal: ActionDraft | None = None


class ActionView(StrictInput):
    view_id: UUID


class ActionInvalidation(ActionView):
    transition_proposal_id: UUID | None = None


class ActionDecision(ActionView):
    proposal_id: UUID
    decision: Literal["approve", "cancel", "select"]
    page_context: PageContext
    sop_id: UUID | None = None


class ActionFeedback(ActionView):
    proposal_id: UUID
    outcome: Literal["succeeded", "failed"]
    error_code: Literal["target_unavailable", "context_changed", "navigation_failed"] | None = None
    destination_context: PageContext | None = None
    destination_view_id: UUID | None = None

    @model_validator(mode="after")
    def check_outcome(self):
        if (self.outcome == "failed") != (self.error_code is not None):
            raise ValueError("Invalid feedback outcome")
        return self
