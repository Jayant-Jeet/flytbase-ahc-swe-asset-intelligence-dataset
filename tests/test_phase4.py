"""Phase 4: visual schema, agent loop (scripted fake provider), API endpoints, provider message conversion."""
import json

import pytest
from fastapi.testclient import TestClient

from app import agent, config, db, ingest, memory, visuals
from app.llm import GeminiProvider, LLMProvider, OpenAICompatProvider


@pytest.fixture(scope="module")
def dbfile(tmp_path_factory):
    f = tmp_path_factory.mktemp("db4") / "t.db"
    ingest.ingest(config.data_dirs(), reset=True, db_file=f)
    return f


@pytest.fixture()
def conn(dbfile):
    c = db.connect(dbfile)
    memory.reset(c)
    yield c
    memory.reset(c)
    c.close()


class Fake(LLMProvider):
    """Scripted provider: each step is a list of events; records what it was sent."""

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def complete(self, system, prompt, json_mode=False):
        return ""

    def stream_chat(self, system, messages, tools):
        self.seen.append((system, [dict(m) for m in messages], [t["name"] for t in tools]))
        yield from self.steps.pop(0)


def call(name, args, i=1):
    return {"type": "tool_call", "id": f"c{i}", "name": name, "args": args, "raw": None}


def test_visual_validation():
    ok = visuals.validate_spec({"type": "bar_chart", "title": "t",
                                "data": {"series": [{"name": "a", "points": [{"x": "W1", "y": 3}]}]}})
    assert ok["type"] == "bar_chart"
    assert visuals.validate_spec(json.dumps({"type": "table", "title": "t", "data": {"columns": ["a"], "rows": [[1]]}}))
    for bad in ({"type": "pie", "title": "t", "data": {}},
                {"type": "map", "title": "t", "data": {"markers": [{"lat": 99, "lon": 0, "label": "x"}]}},
                {"type": "heatmap", "title": "t", "data": {"x_labels": [], "y_labels": ["a"], "values": []}},
                "not json"):
        with pytest.raises(ValueError):
            visuals.validate_spec(bad)


def test_agent_loop_tools_visual_evidence(conn):
    u = conn.execute("SELECT user_id FROM users WHERE name='Ryan Brooks'").fetchone()[0]
    spec = {"type": "status_card", "title": "WELL-0001", "data": {"headline": "Not producing", "producing": False}}
    fake = Fake([
        [call("well_status", {"well_id": "WELL-0001"}, 1), call("render_visual", {"spec": json.dumps(spec)}, 2)],
        [{"type": "text", "text": "WELL-0001 is not producing."}],
    ])
    evs = list(agent.run_turn(conn, fake, u, "status of WELL-0001?"))
    types = [e["type"] for e in evs]
    assert types[0] == "conversation" and types[-1] == "done"
    assert [e["name"] for e in evs if e["type"] == "tool_call"] == ["well_status", "render_visual"]
    assert all(e["ok"] for e in evs if e["type"] == "tool_result")
    assert [e for e in evs if e["type"] == "visual"][0]["spec"]["type"] == "status_card"
    ev = [e for e in evs if e["type"] == "evidence"][0]
    assert "INS-0001-0203" in ev["ids"] and "INS" in ev["groups"]
    # tool results were fed back to the model, and the system prompt carries the user and today
    assert fake.seen[1][1][-1]["role"] == "tool"
    assert "Ryan Brooks" in fake.seen[0][0] and "2026-10-17" in fake.seen[0][0]
    assert "render_visual" in fake.seen[0][2]


def test_user_id_cannot_be_spoofed_by_model(conn):
    ids = {n: conn.execute("SELECT user_id FROM users WHERE name=?", (n,)).fetchone()[0]
           for n in ("Michael Turner", "Jessica Reed")}
    fake = Fake([[call("remember_fact", {"scope_type": "well", "scope_id": "WELL-0007", "text": "shut in for 10 days",
                                         "user_id": ids["Jessica Reed"]})],
                 [{"type": "text", "text": "ok"}]])
    evs = list(agent.run_turn(conn, fake, ids["Michael Turner"], "WELL-0007 is shut in for 10 days"))
    res = [e for e in evs if e["type"] == "tool_result"][0]
    author = conn.execute("SELECT author_user_id FROM facts ORDER BY rowid DESC LIMIT 1").fetchone()
    if res["ok"]:
        assert author[0] == ids["Michael Turner"]


def test_private_history_and_foreign_conversation(conn):
    a = conn.execute("SELECT user_id FROM users WHERE name='Michael Turner'").fetchone()[0]
    b = conn.execute("SELECT user_id FROM users WHERE name='Jessica Reed'").fetchone()[0]
    f1 = Fake([[{"type": "text", "text": "hello A"}]])
    conv = [e for e in agent.run_turn(conn, f1, a, "hi")][0]["id"]
    f2 = Fake([[{"type": "text", "text": "again"}]])
    list(agent.run_turn(conn, f2, a, "second", conv))
    assert [m["content"] for m in f2.seen[0][1]] == ["hi", "hello A", "second"]
    # another user cannot continue it: a new conversation is started with no history
    f3 = Fake([[{"type": "text", "text": "x"}]])
    evs = list(agent.run_turn(conn, f3, b, "peek", conv))
    assert evs[0]["id"] != conv
    assert [m["content"] for m in f3.seen[0][1]] == ["peek"]


def test_provider_exception_is_reported(conn):
    class Boom(Fake):
        def stream_chat(self, *a):
            raise RuntimeError("network down")
            yield
    u = conn.execute("SELECT user_id FROM users LIMIT 1").fetchone()[0]
    evs = list(agent.run_turn(conn, Boom([]), u, "hi"))
    assert any(e["type"] == "error" for e in evs) and evs[-1]["type"] == "done"


def test_gemini_contents_and_openai_messages():
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "raw": [{"functionCall": {"name": "a", "args": {}}}],
             "tool_calls": [{"id": "1", "name": "a", "args": {}}, {"id": "2", "name": "b", "args": {"x": 1}}]},
            {"role": "tool", "tool_call_id": "1", "name": "a", "content": '{"v": 1}'},
            {"role": "tool", "tool_call_id": "2", "name": "b", "content": '{"v": 2}'}]
    g = GeminiProvider._contents(msgs)
    assert g[1]["role"] == "model" and "functionCall" in g[1]["parts"][0]
    assert len(g[2]["parts"]) == 2 and g[2]["parts"][0]["functionResponse"]["response"] == {"v": 1}
    o = OpenAICompatProvider._messages("sys", msgs)
    assert o[0]["role"] == "system" and o[2]["tool_calls"][1]["function"]["arguments"] == '{"x": 1}'
    assert [m["role"] for m in o[3:]] == ["tool", "tool"]
    p = OpenAICompatProvider("x", "http://h", "KEY123", "m")
    assert p._headers["Authorization"] == "Bearer KEY123"


def test_api_endpoints(dbfile, monkeypatch):
    from app import server
    monkeypatch.setattr(config, "db_path", lambda: dbfile)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    c = TestClient(server.app)
    users = c.get("/api/users").json()
    assert users and {"user_id", "name"} <= set(users[0])
    assert c.get("/api/status").json()["wells"] > 0
    assert c.post("/api/chat", json={"user_id": users[0]["user_id"], "message": "hi"}).status_code == 503
    # streaming with a fake provider
    fake = Fake([[{"type": "text", "text": "pong"}]])
    monkeypatch.setattr(server, "get_provider", lambda: fake)
    monkeypatch.setattr(server, "llm_configured", lambda: True)
    r = c.post("/api/chat", json={"user_id": users[0]["user_id"], "message": "ping"})
    events = [json.loads(l[5:]) for l in r.text.split("\n\n") if l.startswith("data:")]
    assert [e["type"] for e in events] == ["conversation", "text", "done"]
    cid = events[0]["id"]
    assert len(c.get(f"/api/conversations/{cid}/messages", params={"user_id": users[0]["user_id"]}).json()) == 2
    assert c.get(f"/api/conversations/{cid}/messages", params={"user_id": users[1]["user_id"]}).status_code == 404
    assert c.post("/api/reset-memory").json()["ok"]
