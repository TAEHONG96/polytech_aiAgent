from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.models import AgentRun
from app.agent.provider import AnalysisFailure, FakeGenerator
from app.agent.queue import process_agent_run, utcnow
from app.companion.tutor import intervention
from app.companion.tutor_models import TutorOffer
from app.db import engine
from app.knowledge.routes import SearchResponse
from app.settings import settings
from app.simulation import resources
from test_companion_actions import BASE, chat_actions, decision, submit
from test_companion_workflows import sop_evidence, ticket_context
from test_simulation import POSTGRES
from test_training_isolation import trainees

ROOT = "/api/v1/companion/tutor"


def tutor_inputs(chat, monkeypatch, *, tab=None):
    monkeypatch.setattr(resources, "settings", lambda: replace(settings(), corpus_training=True))
    monkeypatch.setattr(resources, "run_search", lambda *args, **kwargs: SearchResponse(results=[], embedding_profile="test"))
    context = ticket_context(chat)
    tab = tab or str(uuid4())
    events = []
    for _ in range(2):
        search = chat.client.post(chat.run + "/knowledge/search", headers=chat.headers, json={"query": "합성 검색",
            "source_types": ["SOP"], "ticket_id": context["ticket_id"], "tutor_tab_id": tab})
        assert search.status_code == 200, search.text
        receipt = search.json()["tutor_receipt"]
        assert receipt
        events.append({"kind": "SOP_SEARCH_COMPLETED", "id": str(uuid4()), "at": utcnow().isoformat(), "receipt": receipt})
    return {"page_context": context, "tab_id": tab, "frequency": "high", "active_seconds": 0, "events": events}


def offer_hint(chat, inputs):
    response = chat.client.post(ROOT + "/offers", json=inputs, headers=chat.headers)
    assert response.status_code == 200, response.text
    return response.json()


def command(chat, offer, inputs, name, *, request_id=None, expected=202):
    url = f"{ROOT}/{offer['id']}/hints"
    response = chat.client.post(url, json={"page_context": inputs["page_context"], "tab_id": inputs["tab_id"],
        "command": name, "request_id": request_id or str(uuid4())}, headers=chat.headers)
    assert response.status_code == expected, response.text
    return response.json()


def completed(chat, offer, inputs, hint, model=FakeGenerator):
    assert process_agent_run(model)
    return chat.client.get(f"{ROOT}/{offer['id']}/hints/{hint['id']}?tab_id={inputs['tab_id']}").json()


def test_pattern_weights_exclude_stale_evidence_and_charge_each_pattern_once():
    now = utcnow()
    entries = [("SOP_SEARCH_COMPLETED", 2, 0), ("SOP_SEARCH_COMPLETED", 1, 0), ("SOP_SEARCH_COMPLETED", 0, 0),
               ("ANALYSIS_COMPLETED", 3, None), ("ANALYSIS_FAILED", 2, None), ("TICKET_PROGRESS", 0, None),
               ("SOP_OPENED", 12, None)]
    events = [{"kind": kind, "id": str(uuid4()), "count": count, "at": (now - timedelta(minutes=age)).isoformat()}
              for kind, age, count in entries]
    events[3]["related_sop_ids"] = [events[-1]["id"]]
    score, reasons, _ = intervention(events, 120, now)
    assert score == 7 and set(reasons) == {"empty_sop", "repeated_analysis", "sop_not_observed"}
    events[-1]["at"] = now.isoformat()
    unrelated = {**events[-1], "id": str(uuid4())}
    assert "sop_not_observed" in intervention([*events[:-1], unrelated], 120, now)[1]
    events[2]["count"] = 2
    score, reasons, _ = intervention(events, 120, now)
    assert score == 2 and set(reasons) == {"repeated_analysis"}
    assert intervention([], 90, now)[0] == 1


@POSTGRES
def test_offers_are_verified_and_click_gates_idempotent_generation_and_workflow_consent(chat_actions, sop_evidence, monkeypatch):
    chat = chat_actions
    inputs = tutor_inputs(chat, monkeypatch)
    assert offer_hint(chat, {**inputs, "frequency": "normal"}) is None
    assert offer_hint(chat, {**inputs, "tab_id": str(uuid4())}) is None
    bad = {**inputs["events"][0], "receipt": inputs["events"][0]["receipt"] + "f"}
    assert offer_hint(chat, {**inputs, "events": [bad, inputs["events"][1]]}) is None
    assert offer_hint(chat, {**inputs, "events": [inputs["events"][0]] * 2}) is None
    offer = offer_hint(chat, inputs)
    assert offer["score"] == 3 and offer["rules"] == ["empty_sop"]
    with Session(engine()) as db:
        row = db.get(TutorOffer, UUID(offer["id"]))
        assert row.data["history"][0]["event"] == "OFFERED" and not row.data["generations"]
        assert db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.companion["tutor_hint"]["id"].astext == offer["id"])) == 0
    assert command(chat, offer, {**inputs, "tab_id": str(uuid4())}, "view", expected=404)["error"]["code"] == "TUTOR_OFFER_NOT_FOUND"
    wrong = {**inputs, "page_context": {**inputs["page_context"], "section": "analysis"}}
    assert command(chat, offer, wrong, "view", expected=409)["error"]["code"] == "TUTOR_CONTEXT_CHANGED"
    click_id = str(uuid4())
    initial = command(chat, offer, inputs, "view", request_id=click_id)
    assert initial["id"] == command(chat, offer, inputs, "view", request_id=click_id)["id"]
    level1 = completed(chat, offer, inputs, initial)
    assert level1["level"] == 1 and level1["execution_mode"] == "mock" and level1["model_calls"] == 1
    # A linked natural-language request uses the very same hint series.
    chat_body = {"conversation_id": chat.conversation, "request_id": str(uuid4()), "message": "조금 더 알려줘",
        "page_context": inputs["page_context"], "action_view_id": str(uuid4()), "tutor_offer_id": offer["id"], "tutor_tab_id": inputs["tab_id"]}
    response = chat.client.post(BASE, json=chat_body, headers=chat.headers)
    assert response.status_code == 202, response.text
    assert response.json()["tutor_level"] == 2
    assert chat.client.post(BASE, json=chat_body, headers=chat.headers).json()["message_id"] == response.json()["message_id"]
    level2 = completed(chat, offer, inputs, {"id": response.json()["message_id"]})
    level3 = completed(chat, offer, inputs, command(chat, offer, inputs, "more"))
    assert [level1["level"], level2["level"], level3["level"]] == [1, 2, 3]
    assert len({level1["answer"], level2["answer"], level3["answer"]}) == 3
    assert command(chat, offer, inputs, "more", expected=409)["error"]["code"] == "TUTOR_LEVEL_LIMIT"
    with Session(engine()) as db:
        assert not list(db.scalars(select(AgentRun).where(AgentRun.companion["workflow_kind"].astext.is_not(None),
                AgentRun.requested_by == db.get(TutorOffer, UUID(offer["id"])).user_id)))
    accepted = chat.client.post(BASE, json={**chat_body, "request_id": str(uuid4()), "message": "응, 찾아줘."}, headers=chat.headers)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["workflow"]["kind"] == "sop_compare"
    assert process_agent_run(FakeGenerator)
    with Session(engine()) as db:
        assert db.get(TutorOffer, UUID(offer["id"])).data["history"][-1]["event"] == "WORKFLOW_REQUESTED"
    followup = chat.client.post(BASE, json={**chat_body, "request_id": str(uuid4()), "message": "POST와 BMC의 차이점은?"}, headers=chat.headers)
    assert followup.status_code == 202, followup.text
    assert process_agent_run(FakeGenerator)
    assert chat.client.get(chat.url).json()["turns"][-1]["status"] == "succeeded"


@POSTGRES
def test_tutor_expiry_busy_and_bounded_retry(chat_actions, monkeypatch):
    class Unavailable(FakeGenerator):
        async def generate(self, contents, config):
            raise AnalysisFailure("AI_UNAVAILABLE", "합성 연결 오류", retryable=True)

    chat = chat_actions
    inputs = tutor_inputs(chat, monkeypatch)
    other_run = chat.client.post(BASE, headers=chat.headers, json={"message": "POST와 BMC의 차이점은?",
        "page_context": inputs["page_context"], "conversation_id": str(uuid4()), "request_id": str(uuid4())})
    assert other_run.status_code == 202
    assert offer_hint(chat, inputs) is None
    assert process_agent_run(FakeGenerator)
    offer = offer_hint(chat, inputs)
    assert chat.client.post(f"{ROOT}/{offer['id']}/feedback", headers=chat.headers,
        json={"tab_id": inputs["tab_id"], "event": "EXPIRED"}).status_code == 204
    assert command(chat, offer, inputs, "view", expected=409)["error"]["code"] == "TUTOR_OFFER_EXPIRED"
    assert offer_hint(chat, inputs) is None
    fresh = tutor_inputs(chat, monkeypatch)
    retryable = offer_hint(chat, fresh)
    failed = completed(chat, retryable, fresh, command(chat, retryable, fresh, "view"), Unavailable)
    assert failed["execution_mode"] == "error" and not failed["answer"] and failed["model_calls"] == 1 and failed["can_retry"]
    exhausted = completed(chat, retryable, fresh, command(chat, retryable, fresh, "retry"), Unavailable)
    assert exhausted["execution_mode"] == "error" and exhausted["model_calls"] == 1 and not exhausted["can_retry"]
    assert command(chat, retryable, fresh, "retry", expected=409)["error"]["code"] == "TUTOR_RETRY_LIMIT"


@POSTGRES
def test_pending_approval_workflow_and_repeated_dismissal_suppress_offers(chat_actions, monkeypatch):
    chat = chat_actions
    inputs = tutor_inputs(chat, monkeypatch)
    proposal = submit(chat, "SOP 검색 버튼이 어디 있어?", context=inputs["page_context"])
    assert proposal["action_proposal"]["status"] == "pending" and offer_hint(chat, inputs) is None
    url, payload = decision(chat, proposal)
    assert chat.client.post(url, headers=chat.headers, json={**payload, "decision": "cancel"}).status_code == 200
    workflow = submit(chat, "티켓 검색 화면으로 이동한 다음 검색창을 찾아줘.", context=inputs["page_context"])
    assert workflow["workflow"]["status"] == "WAITING_APPROVAL" and offer_hint(chat, inputs) is None
    url, payload = decision(chat, workflow)
    assert chat.client.post(url, headers=chat.headers, json={**payload, "decision": "cancel"}).status_code == 200
    for _ in range(2):
        offer = offer_hint(chat, inputs)
        assert offer
        assert chat.client.post(f"{ROOT}/{offer['id']}/feedback", headers=chat.headers,
            json={"tab_id": inputs["tab_id"], "event": "DISMISSED"}).status_code == 204
        with Session(engine()) as db, db.begin():
            db.get(TutorOffer, UUID(offer["id"])).created_at -= timedelta(seconds=100)
        inputs = tutor_inputs(chat, monkeypatch, tab=inputs["tab_id"])
    assert offer_hint(chat, inputs) is None


@POSTGRES
def test_new_scope_invalidates_a_ready_offer_without_reissuing_the_same_issue(chat_actions, monkeypatch):
    inputs = tutor_inputs(chat_actions, monkeypatch)
    original = offer_hint(chat_actions, inputs)
    next_view = {**inputs, "page_context": {**inputs["page_context"], "section": "analysis"}}
    assert offer_hint(chat_actions, next_view) is None
    assert command(chat_actions, original, inputs, "view", expected=409)["error"]["code"] == "TUTOR_OFFER_EXPIRED"
    with Session(engine()) as db:
        assert db.get(TutorOffer, UUID(original["id"])).data["status"] == "INVALIDATED"
