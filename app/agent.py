"""Agent loop: the LLM picks tools and writes prose; all facts come from tool results.

`run_turn` is a generator of UI events:
  conversation {id} | text {text} | tool_call {name,args} | tool_result {name,ok,summary,evidence_ids}
  visual {spec} | evidence {ids,groups,docs} | error {message} | done {}
"""
from __future__ import annotations

import json
import re
from typing import Iterator

from . import config, memory
from .llm import LLMProvider
from .tools import TOOLS, call_tool, tool_specs

MAX_STEPS = 10
MAX_TOOL_CHARS = 14000
ID_PREFIX = re.compile(r"^(INS|MNT|CTX|REG|FACT|COR|CONV)-")

SYSTEM = """You are the Asset Intelligence Copilot for an oil field operator. You answer questions about wells using tools.

Today is {today}. The current user is {user_name} ({user_id}); "I", "me", "my" refer to them.

RULES
1. Every fact, count, sum, date or threshold MUST come from a tool result. Never compute, estimate or recall numbers yourself. If a tool does not give it, say you do not have it.
2. Inspections are the truth about whether a well produces; the recorded_status field is often stale. Say so when they differ.
3. Downtime, spills and costs come from the downtime, spills and repair_costs tools (corrections already applied). Pass relative periods (e.g. last_4_months) as the `period`; never calculate dates.
4. Never invent a cause. If a not-producing well has no recorded reason, say "no recorded reason" and suggest a concrete check (e.g. inspect the pump jack / confirm with the field team).
5. User statements: when a user tells you something about a well (shut in, back online, planned work) call remember_fact with state/since/duration_days. When a user gives a rule for a well type call remember_fact with scope_type=well_type, rule_key and rule_value. When a user corrects a measured or derived value call record_correction. Then relay exactly what the tool returned: if it reports conflicts (needs_confirmation=true), do NOT pick a side. Tell the user what the data shows, name both sources (record ids and who said it), say the claim is stored as unconfirmed, and ask them to confirm or withdraw (confirm_fact).
6. When an answer depends on a user-defined rule or a correction, say so explicitly ("following Michael Turner's rule, not the manual", "using the corrected value; original was X, corrected by Y because Z"). Mention a conflict between a correction and an inspector note when the tool reports one.
7. Shared facts about a well (shared_facts, user_shutdowns) come from colleagues: attribute them by name and status.
8. "Wells we discussed yesterday" and similar: call recall. If it finds nothing, say there is no such earlier conversation. Never use another user's history.
9. For questions about a spill being real, use explain_spill and present each note with its author; a note is not a correction.
10. Visuals: after fetching data call render_visual once when a visual helps. Use the type the user names; otherwise list of records -> table, change over time -> line_chart, compare a few wells or areas -> bar_chart, locations -> map, two dimensions (area x month) -> heatmap, one well's state -> status_card, a single fact -> text only. Copy numbers exactly from tool results.
11. Be concise: lead with the answer, then the key supporting facts. Mention record ids (INS-, MNT-, CTX-, REG-, FACT-, COR-) inline for the important claims. An Evidence list is added automatically; do not write one yourself.
12. Use search_docs / get_rules for questions about procedures, limits or manuals and cite the section. Use sql_query only if no other tool fits.
13. Missing data: if a question needs data the dataset may not have (reservoir pressure, oil volumes produced), call data_inventory first. If the field is absent say so plainly, never fabricate or silently substitute; offer the closest proxies the data does have and ask which to use.
14. Provenance: for each key fact say where it came from (inspection, maintenance report, operator note, regulator letter, SOP/MOC memo, supplier notice, a user's claim). Say when sources disagree and which is newer. A user's claim (e.g. "pump replaced") is attributed, marked unverified, and never removes a well from a list; a second-hand claim is unconfirmed and says who told the user.
15. Private preferences ("sort by area then well ID", "costs in thousands") are saved with set_preference for this user only and applied by tools to that user's later answers. Never apply or reveal them for other users. "My area/wells" means the current user's team (scope "my area").
16. Follow-up turns in one conversation continue the same context; reuse earlier results rather than asking again. For ambiguous words (e.g. "hotter": ambient vs wellhead) report both readings and state the assumption.
17. Visuals: a table when the user wants records or a list, a stacked bar (stacked:true) for a breakdown of parts of a whole per category, a map for locations. If the user names a visual, render that type.
18. Operational questions have dedicated tools: gas_odor_responses, temporary_bypasses, status_conflicts, regulator_letter_spills, recalled_pumps, team_info, freeze_protection_check, repair_delay_reasons, area_temperature_comparison, operator_complaints. Prefer them over sql_query; they read the SOP/MOC/notice parameters from the documents.
"""


def _shrink(obj, depth=0):
    """Trim long lists so tool results stay within the model's context."""
    if isinstance(obj, list):
        if len(obj) > 40:
            return [_shrink(x, depth + 1) for x in obj[:40]] + [{"_truncated": f"{len(obj) - 40} more items not shown"}]
        return [_shrink(x, depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {k: _shrink(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, str) and len(obj) > 1500:
        return obj[:1500] + "…"
    return obj


def _tool_message(result: dict) -> str:
    s = json.dumps(_shrink(result), default=str)
    if len(s) > MAX_TOOL_CHARS:
        slim = {k: v for k, v in result.items() if k not in ("points", "rows", "items")}
        s = json.dumps(_shrink(slim), default=str)[:MAX_TOOL_CHARS]
    return s


def _summary(name: str, res: dict) -> str:
    if "error" in res:
        return f"error: {res['error']}"
    for k in ("wells", "spills", "points", "results", "triggers", "rows", "items"):
        if isinstance(res.get(k), list):
            return f"{len(res[k])} {k}"
    if name == "render_visual":
        return f"{res.get('visual', {}).get('type')} rendered"
    return "ok"


def _history(conn, conv_id: str, user_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT role, content FROM messages WHERE conv_id=? AND user_id=? AND role IN ('user','assistant') "
                        "AND content IS NOT NULL ORDER BY msg_id DESC LIMIT ?", (conv_id, user_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _evidence(ids: list[str], docs: dict[str, dict]) -> dict:
    ids = list(dict.fromkeys(i for i in ids if isinstance(i, str)))[:80]
    groups: dict[str, list[str]] = {}
    for i in ids:
        m = ID_PREFIX.match(i)
        groups.setdefault(m.group(1) if m else "DOC", []).append(i)
    return {"ids": ids, "groups": groups, "docs": [docs[i] for i in ids if i in docs]}


def run_turn(conn, provider: LLMProvider, user_id: str, message: str, conv_id: str | None = None) -> Iterator[dict]:
    user = conn.execute("SELECT user_id, name FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        yield {"type": "error", "message": f"unknown user {user_id!r}"}
        return
    if conv_id:
        owner = conn.execute("SELECT user_id FROM conversations WHERE conv_id=?", (conv_id,)).fetchone()
        if not owner or owner[0] != user_id:
            conv_id = None   # never continue someone else's conversation
    if not conv_id:
        conv_id = memory.start_conversation(conn, user_id, message[:60])
    yield {"type": "conversation", "id": conv_id}

    messages = _history(conn, conv_id, user_id) + [{"role": "user", "content": message}]
    memory.log_message(conn, conv_id, user_id, "user", message)
    system = SYSTEM.format(today=memory.logical_today(conn).isoformat(), user_name=user["name"], user_id=user_id)
    specs = tool_specs()
    evidence: list[str] = []
    docs: dict[str, dict] = {}
    final_text = ""

    try:
        for _ in range(MAX_STEPS):
            text, calls = "", []
            for ev in provider.stream_chat(system, messages, specs):
                if ev["type"] == "text":
                    text += ev["text"]
                    yield {"type": "text", "text": ev["text"]}
                else:
                    calls.append(ev)
            final_text += text
            if not calls:
                break
            messages.append({"role": "assistant", "content": text, "raw": [c["raw"] for c in calls if c.get("raw")] or None,
                             "tool_calls": [{"id": c["id"], "name": c["name"], "args": c["args"]} for c in calls]})
            for c in calls:
                yield {"type": "tool_call", "name": c["name"], "args": c["args"]}
                res = call_tool(conn, c["name"], c["args"], user_id=user_id, conv_id=conv_id)
                ok = "error" not in res
                if ok and c["name"] == "render_visual":
                    yield {"type": "visual", "spec": res["visual"]}
                ids = res.get("evidence_ids", []) if isinstance(res, dict) else []
                evidence += ids
                for h in res.get("results", []) if c["name"] == "search_docs" and ok else []:
                    docs[h["chunk_id"]] = {"id": h["chunk_id"], "doc_type": h.get("doc_type"), "section": h.get("section"),
                                           "doc_id": h.get("doc_id")}
                yield {"type": "tool_result", "name": c["name"], "ok": ok, "summary": _summary(c["name"], res),
                       "evidence_ids": ids[:20]}
                messages.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": _tool_message(res)})
            if final_text:
                final_text += "\n\n"
        else:
            note = "\n\n(I stopped after too many tool steps; please narrow the question.)"
            final_text += note
            yield {"type": "text", "text": note}
    except Exception as e:  # provider or network failure: surface it, keep the conversation usable
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}

    memory.log_message(conn, conv_id, user_id, "assistant", final_text.strip() or "(no answer)")
    ev = _evidence(evidence, docs)
    if ev["ids"]:
        yield {"type": "evidence", **ev}
    yield {"type": "done"}
