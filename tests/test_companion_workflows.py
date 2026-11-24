from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from google.genai import types
from sqlalchemy.orm import Session

from app.agent.provider import FakeGenerator, reply
from app.agent.models import AgentRun
from app.agent.queue import process_agent_run
from app.agent.schemas import Evidence
from app.companion.schemas import PageContext
from app.companion import routes
from app.companion.workflow_worker import Toolbox
from app.companion.workflows import finish_workflow, initial_workflow, plan_template, requested_workflow
from app.db import engine
from app.knowledge.models import IndexJob, SopDocument
from test_companion_actions import BASE, chat_actions, decision, submit
from test_simulation import POSTGRES
from test_training_isolation import first_ticket, trainees

GUIDE = "티켓 검색 화면으로 이동한 다음 검색창을 찾아줘."
REVIEW = "지금 티켓을 분석하고 관련 SOP를 찾아서 다음 작업을 알려줘."
OPEN = "관련 SOP를 찾아서 열어줘."
COMPARE = "현재 티켓에 맞는 SOP를 찾아서 어떤 문서가 가장 관련성이 높은지 설명해 줘."
COMPARE_OPEN = "현재 티켓에 맞는 SOP를 찾아서 어떤 문서가 가장 관련성이 높은지 설명해 주고, 내가 선택한 문서를 열어줘."
HANDOVER = "인수인계 페이지로 이동해서 어떤 항목을 확인해야 하는지 설명해 줘."
HIGHLIGHT = "발신 인계로 이동해서 중요한 부분을 화면에서 안내하고 미완료 업무를 확인할 항목을 알려줘."


def ticket_context(chat):
    return {**chat.context, "page_id": "ticket", "ticket_id": first_ticket(chat.client, chat.run)["id"]}


def control(chat, turn, command, *, context=None, view=None, clue=None, expected=200):
    response = chat.client.post(f"{chat.url}/workflows/{turn['message_id']}", headers=chat.headers,
                                json={"command": command, "page_context": context or chat.context,
                                      "view_id": view or str(uuid4()), "clue": clue})
    assert response.status_code == expected, response.text
    return response.json()


def latest(chat):
    return chat.client.get(chat.url).json()["turns"][-1]


@pytest.mark.parametrize(("message", "kind"), [
    (REVIEW, "ticket_review"), (OPEN, "sop_open"), (GUIDE, "ticket_search"),
    (COMPARE, "sop_compare"), (COMPARE_OPEN, "sop_compare_open"), (HANDOVER, "handover_guide"), (HIGHLIGHT, "handover_highlight"),
    ("인수인계 페이지로 이동해 줘", None), ("SOP의 역할을 설명해 줘", None),
    ("이 티켓을 분석하고 관련 SOP를 찾아서 다음 작업을 알려줘. 그리고 SOP를 열어줘.", "ticket_review_open"),
    ("이 장애와 관련된 SOP를 찾아줘.", None), ("POST와 BMC의 차이점은?", None),
    ("SOP를 찾아서 열어줘. 그리고 SSH 명령 실행", None),
])
def test_workflow_intent_is_distinct_from_chat_and_single_action(message, kind):
    context = PageContext(page_id="ticket", run_id=uuid4(), ticket_id=uuid4())
    assert requested_workflow(message, context) == kind
    if kind:
        assert 2 <= len(plan_template(kind, context)) <= 5


@pytest.fixture
def sop_evidence(chat_actions, monkeypatch):
    chat = chat_actions
    ids = [uuid4(), uuid4()]
    now = datetime.now(timezone.utc)
    with Session(engine()) as db, db.begin():
        for index, doc in enumerate(ids):
            db.add(SopDocument(id=doc, title=f"승인 합성 SOP {index}", filename="workflow.md", content_type="text/markdown",
                               content_sha256=doc.hex * 2, size_bytes=12, training_approved=True, created_by=chat.owner_id,
                               created_at=now, updated_at=now))
            db.flush()
            db.add(IndexJob(id=doc, source_type="SOP", sop_id=doc, status="COMPLETED", attempts=0,
                            available_at=now, updated_at=now))

    async def search(self, name, args):
        assert name == "search_sop"
        for index, doc in enumerate(ids):
            source = Evidence(key=f"K{index + 1}", kind="SOP", source_id=doc, title=f"승인 합성 SOP {index}",
                              content="합성 관측값", source_url=f"{chat.run}/sops/{doc}")
            self.evidence[source.key] = source
        return SimpleNamespace(status="ok")

    monkeypatch.setattr(Toolbox, "execute", search)
    return ids


@POSTGRES
def test_ticket_review_is_ordered_and_only_reports_scoped_sources(chat_actions, sop_evidence):
    chat = chat_actions
    turn = submit(chat, REVIEW, context=ticket_context(chat))
    flow = turn["workflow"]
    assert flow["status"] == "COMPLETED"
    assert [step["type"] for step in flow["steps"]] == ["read_ticket", "search_sop", "analyze", "report"]
    assert all(step["status"] == "COMPLETED" and step["started_at"] <= step["finished_at"] for step in flow["steps"])
    assert flow["model_calls"] == 2 and flow["embedding_calls"] == 1
    assert "관측된 사실" in turn["answer"] and "다음 확인 항목" in turn["answer"]
    assert {source["source_id"] for source in turn["sources"]} == {str(doc) for doc in sop_evidence}
    assert "data" not in flow and turn["action_proposal"] is None


@POSTGRES
def test_sop_workflow_requires_choice_and_verifies_destination(chat_actions, sop_evidence):
    chat = chat_actions
    turn = submit(chat, OPEN, context=ticket_context(chat))
    assert turn["workflow"]["status"] == "WAITING_APPROVAL"
    assert turn["workflow"]["model_calls"] == 1
    url, request = decision(chat, turn)
    assert chat.client.post(url, json=request, headers=chat.headers).status_code == 409
    selected = str(sop_evidence[1])
    approved = chat.client.post(url, json={**request, "sop_id": selected}, headers=chat.headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["command"]["sop_id"] == selected
    payload = {"proposal_id": request["proposal_id"], "view_id": request["view_id"], "outcome": "succeeded",
               "destination_view_id": str(uuid4()), "destination_context": {**chat.context, "page_id": "sop", "sop_id": str(sop_evidence[0])}}
    assert chat.client.post(url + "/feedback", json=payload, headers=chat.headers).status_code == 409
    payload["destination_context"]["sop_id"] = selected
    assert chat.client.post(url + "/feedback", json=payload, headers=chat.headers).status_code == 204
    assert chat.client.post(url + "/feedback", json=payload, headers=chat.headers).status_code == 204
    result = latest(chat)
    assert result["workflow"]["status"] == "COMPLETED" and "승인 합성 SOP 1" in result["answer"]


@POSTGRES
def test_navigation_feedback_issues_a_fresh_focus_approval_and_preserves_progress(chat_actions):
    chat = chat_actions
    turn = submit(chat, GUIDE)
    endpoint, first = decision(chat, turn)
    assert turn["action_proposal"]["type"] == "navigate_to_page"
    assert chat.client.post(endpoint, json=first, headers=chat.headers).status_code == 200
    chat.client.post(chat.url + "/actions/invalidate", json={"view_id": first["view_id"],
                     "transition_proposal_id": first["proposal_id"]}, headers=chat.headers)
    destination = {**chat.context, "section": "tickets"}
    view = str(uuid4())
    receipt = {"proposal_id": first["proposal_id"], "view_id": first["view_id"], "outcome": "succeeded",
               "destination_view_id": view, "destination_context": destination}
    assert chat.client.post(endpoint + "/feedback", json=receipt, headers=chat.headers).status_code == 204
    assert chat.client.post(endpoint + "/feedback", json=receipt, headers=chat.headers).status_code == 204
    next_turn = latest(chat)
    assert next_turn["workflow"]["current_step_index"] == 2
    assert next_turn["action_proposal"]["type"] == "focus_input" and next_turn["action_proposal"]["view_id"] == view
    assert next_turn["action_proposal"]["id"] != first["proposal_id"]
    assert chat.client.post(endpoint, json=first, headers=chat.headers).status_code == 404
    _, second = decision(chat, next_turn)
    second["page_context"] = destination
    assert chat.client.post(endpoint, json=second, headers=chat.headers).json()["command"]["anchor_id"] == "ticket-search"
    assert chat.client.post(endpoint + "/feedback", json={"proposal_id": second["proposal_id"], "view_id": view,
        "outcome": "succeeded"}, headers=chat.headers).status_code == 204
    assert latest(chat)["workflow"]["status"] == "COMPLETED"


@POSTGRES
def test_sop_comparison_and_explicit_selection_are_separate_from_approval(chat_actions, sop_evidence):
    chat = chat_actions
    context = ticket_context(chat)
    explained = submit(chat, COMPARE, context=context)
    assert explained["workflow"]["status"] == "COMPLETED" and explained["action_proposal"] is None
    assert {entry["sop_id"] for entry in explained["sop_comparison"]["candidates"]} == {str(item) for item in sop_evidence}
    assert all(entry["scope"] == "합성 관측값" for entry in explained["sop_comparison"]["candidates"])
    choosing = submit(chat, COMPARE_OPEN, context=context)
    assert choosing["workflow"]["input_kind"] == "sop_selection" and choosing["sop_comparison"]["reason"]
    endpoint, identity = decision(chat, choosing)
    first, second = map(str, sop_evidence)
    assert chat.client.post(endpoint, json={**identity, "sop_id": second}, headers=chat.headers).status_code == 409
    select = chat.client.post(endpoint, json={**identity, "decision": "select", "sop_id": second}, headers=chat.headers)
    assert select.json() == {"status": "selected", "command": None}
    turn = latest(chat)
    assert turn["workflow"]["status"] == "WAITING_APPROVAL"
    assert {goal["id"]: goal["status"] for goal in turn["workflow"]["goal_requirements"]}["USER_SELECT_SOP"] == "COMPLETED"
    assert chat.client.post(endpoint, json={**identity, "sop_id": first}, headers=chat.headers).status_code == 409
    assert chat.client.post(endpoint, json={**identity, "sop_id": second}, headers=chat.headers).status_code == 200
    assert chat.client.post(endpoint + "/feedback", json={"proposal_id": identity["proposal_id"], "view_id": identity["view_id"],
        "outcome": "succeeded", "destination_view_id": str(uuid4()), "destination_context": {
            **chat.context, "page_id": "sop", "sop_id": second}}, headers=chat.headers).status_code == 204
    final = latest(chat)
    assert final["workflow"]["status"] == "COMPLETED" and final["workflow"]["outcome"]["incomplete"] == []
    assert final["sop_comparison"] == choosing["sop_comparison"]


@POSTGRES
def test_handover_feedback_queues_explanation_and_preserves_it_on_missing_anchor(chat_actions):
    chat = chat_actions
    turn = submit(chat, HIGHLIGHT)
    endpoint, navigation = decision(chat, turn)
    assert chat.client.post(endpoint, json=navigation, headers=chat.headers).json()["command"]["route_id"] == "outgoing_handover"
    destination = {**chat.context, "section": "outgoing"}
    view = str(uuid4())
    receipt = {"proposal_id": navigation["proposal_id"], "view_id": navigation["view_id"], "outcome": "succeeded",
               "destination_view_id": view, "destination_context": {**destination, "section": "tickets"}}
    assert chat.client.post(endpoint + "/feedback", json=receipt, headers=chat.headers).status_code == 409
    receipt["destination_context"] = destination
    assert chat.client.post(endpoint + "/feedback", json=receipt, headers=chat.headers).status_code == 204
    assert latest(chat)["workflow"]["status"] == "RUNNING"
    assert chat.client.post(endpoint + "/feedback", json=receipt, headers=chat.headers).status_code == 204
    assert process_agent_run(FakeGenerator)
    proposed = latest(chat)
    assert proposed["workflow"]["model_calls"] == 2 and proposed["workflow"]["status"] == "WAITING_APPROVAL"
    assert "이전 Bot" in proposed["answer"] and "티켓별 다음 조치" in proposed["answer"]
    _, approval = decision(chat, proposed)
    approval["page_context"] = destination
    assert chat.client.post(endpoint, json=approval, headers=chat.headers).json()["command"]["anchor_id"] == "outgoing-handover"
    assert chat.client.post(endpoint + "/feedback", json={"proposal_id": approval["proposal_id"], "view_id": view,
        "outcome": "failed", "error_code": "target_unavailable"}, headers=chat.headers).status_code == 204
    final = latest(chat)
    assert final["workflow"]["status"] == "FAILED" and final["answer"] == proposed["answer"]
    assert final["workflow"]["outcome"]["incomplete"] == ["발신 인계 섹션 강조"]


@POSTGRES
def test_late_approved_navigation_receipt_does_not_start_any_followup(chat_actions, monkeypatch):
    chat = chat_actions
    turn = submit(chat, HANDOVER)
    endpoint, approval = decision(chat, turn)
    assert chat.client.post(endpoint, json=approval, headers=chat.headers).status_code == 200
    expired = datetime.fromisoformat(turn["action_proposal"]["expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(routes, "utcnow", lambda: expired)
    assert chat.client.post(endpoint + "/feedback", headers=chat.headers, json={"proposal_id": approval["proposal_id"],
        "view_id": approval["view_id"], "outcome": "succeeded", "destination_view_id": str(uuid4()),
        "destination_context": {**chat.context, "section": "outgoing"}}).status_code == 409
    result = latest(chat)
    assert result["workflow"]["status"] == "PAUSED" and result["workflow"]["current_step_index"] == 0
    assert result["workflow"]["model_calls"] == 1 and result["answer"] is None


def test_completion_validator_rejects_missing_explanation_even_with_successful_ui():
    context = PageContext(page_id="workspace", section="outgoing", run_id=uuid4())
    flow = initial_workflow("handover_highlight", context, str(uuid4()), datetime.now(timezone.utc))
    flow["steps"] = [
        {"id": 1, "type": "explain_handover", "status": "COMPLETED", "label": "설명", "result": {"explained": True}},
        {"id": 2, "type": "highlight_handover", "status": "COMPLETED", "label": "강조", "result": {"anchor_id": "outgoing-handover"},
         "approved_proposal_id": str(uuid4())},
    ]
    finish_workflow({"workflow": flow})
    assert flow["status"] == "FAILED" and flow["outcome"]["incomplete"] == ["설명"]


@POSTGRES
def test_pause_resume_cancel_and_one_active_workflow(chat_actions):
    chat = chat_actions
    turn = submit(chat, GUIDE)
    endpoint, request = decision(chat, turn)
    response = chat.client.post(BASE, json={"message": GUIDE, "page_context": chat.context, "action_view_id": str(uuid4()),
        "conversation_id": str(uuid4()), "request_id": str(uuid4())}, headers=chat.headers)
    assert response.json()["error"]["code"] == "WORKFLOW_BUSY"
    response = chat.client.post(chat.url + "/actions/invalidate", json={"view_id": request["view_id"]}, headers=chat.headers)
    assert response.status_code == 204 and latest(chat)["workflow"]["status"] == "PAUSED"
    assert chat.client.post(endpoint, json=request, headers=chat.headers).status_code == 409
    control(chat, turn, "resume", context={**chat.context, "section": "tickets"}, expected=409)
    assert control(chat, turn, "resume")["status"] == "queued"
    assert control(chat, turn, "resume", expected=409)["error"]["code"] == "WORKFLOW_NOT_RESUMABLE"
    assert process_agent_run(FakeGenerator)
    resumed = latest(chat)
    assert resumed["workflow"]["model_calls"] == 1 and resumed["workflow"]["steps"][0]["status"] == "COMPLETED"
    assert resumed["action_proposal"]["id"] != request["proposal_id"]
    cancelled = control(chat, resumed, "cancel")
    assert cancelled["workflow"]["status"] == "CANCELLED"
    assert [step["status"] for step in cancelled["workflow"]["steps"]] == ["COMPLETED", "SKIPPED", "SKIPPED"]


@POSTGRES
def test_empty_evidence_stops_and_only_retries_the_failed_step(chat_actions, monkeypatch):
    async def empty(self, name, args):
        return SimpleNamespace(status="ok")

    monkeypatch.setattr(Toolbox, "execute", empty)
    chat = chat_actions
    context = ticket_context(chat)
    turn = submit(chat, REVIEW, context=context)
    assert turn["workflow"]["status"] == "WAITING_USER_INPUT" and turn["action_proposal"] is None
    assert control(chat, turn, "resume", context=context, expected=422)["error"]["code"] == "WORKFLOW_CLUE_REQUIRED"
    control(chat, turn, "resume", context=context, clue="합성 ECC")
    assert process_agent_run(FakeGenerator)
    flow = latest(chat)["workflow"]
    assert flow["status"] == "FAILED" and not flow["can_resume"]
    assert [step["attempts"] for step in flow["steps"]] == [1, 2, 0, 0]
    assert flow["model_calls"] == 1


@POSTGRES
def test_wrong_model_plan_is_stopped_without_execution(chat_actions):
    class InvalidPlan(FakeGenerator):
        async def generate(self, contents, config):
            return reply(types.Part(text='{"steps":["focus_ticket_search","explain_search"]}'))

    turn = submit(chat_actions, GUIDE, generator=InvalidPlan)
    assert turn["workflow"]["error_code"] == "WORKFLOW_INVALID_PLAN"
    assert turn["workflow"]["status"] == "FAILED" and turn["workflow"]["steps"] == []
    assert turn["action_proposal"] is None


@POSTGRES
def test_worker_lease_recovery_and_workflow_expiration(chat_actions, monkeypatch):
    chat = chat_actions
    response = chat.client.post(BASE, json={"message": GUIDE, "page_context": chat.context, "action_view_id": str(uuid4()),
        "conversation_id": chat.conversation, "request_id": str(uuid4())}, headers=chat.headers)
    assert response.status_code == 202
    queued = response.json()
    with Session(engine()) as db, db.begin():
        running = db.get(AgentRun, UUID(queued["message_id"]))
        running.status = "running"
        running.lease_token = uuid4()
        running.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not process_agent_run(FakeGenerator)
    interrupted = latest(chat)
    assert interrupted["workflow"]["status"] == "FAILED" and interrupted["workflow"]["can_resume"]
    assert interrupted["workflow"]["error_code"] == "AI_WORKER_INTERRUPTED"
    control(chat, interrupted, "resume")
    assert process_agent_run(FakeGenerator)
    proposed = latest(chat)
    assert proposed["workflow"]["status"] == "WAITING_APPROVAL" and proposed["workflow"]["model_calls"] == 1
    endpoint, body = decision(chat, proposed)
    monkeypatch.setattr(routes, "utcnow", lambda: datetime.now(timezone.utc) + timedelta(minutes=11))
    assert latest(chat)["workflow"]["status"] == "EXPIRED"
    assert chat.client.post(endpoint, json=body, headers=chat.headers).status_code == 409
    assert control(chat, proposed, "resume", expected=409)["error"]["code"] == "WORKFLOW_NOT_RESUMABLE"
