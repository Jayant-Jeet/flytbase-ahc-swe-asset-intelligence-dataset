"""Evaluation harness for the 18 example questions (03-evaluation-queries.md).

  python -m tests.eval_questions                 # tool-level: asserts the facts the tools return (no LLM, deterministic)
  python -m tests.eval_questions --llm           # end to end through the agent and the configured model
  python -m tests.eval_questions --only 12 14    # run selected questions (note: Q11-Q18 depend on earlier memory steps)
  python -m tests.eval_questions --data-dir PATH # evaluate another dataset (expected values are for the sample data)

Memory is reset at the start of every run, and the flows follow the order of the question sheet
(Q11 day 1 -> next day -> Q11 day 2; Q12/Q13 corrections -> Q15; Q16 recall for another user).
Every run uses a fresh temporary database, so it never touches your app database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from app import config, db, ingest, memory
from app.tools import call_tool

Checks = list[tuple[str, bool]]


class Ctx:
    def __init__(self, conn):
        self.conn = conn

    def uid(self, name: str) -> str:
        return self.conn.execute("SELECT user_id FROM users WHERE name=?", (name,)).fetchone()[0]

    def tool(self, name, args, who=None, conv=None):
        return call_tool(self.conn, name, args, user_id=self.uid(who) if who else None, conv_id=conv)


def approx(a, b, tol=0.06):
    return a is not None and abs(a - b) <= tol


# ---------------------------------------------------------------- tool-level (deterministic) checks
def q1(c: Ctx) -> Checks:
    r = c.tool("well_status", {"well_id": "WELL-0001"})
    li = r["latest_inspection"]
    return [("not producing", r["producing"] is False),
            ("INS-0001-0203 by Ryan Brooks", li["inspection_id"] == "INS-0001-0203" and li["inspector_name"] == "Ryan Brooks"),
            ("delta 3.2 C", li["delta_c"] == 3.2),
            ("note CTX-0001-003 planned shut-in", [n["note_id"] for n in r["planned_shutdowns"]] == ["CTX-0001-003"]),
            ("recorded status stale", r["recorded_status"] == "active" and bool(r["recorded_status_out_of_date"]))]


def q2(c: Ctx, expect=("WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175")) -> Checks:
    r = c.tool("list_wells_not_producing", {"scope": "Northeast Block 8"})
    ws = {w["well_id"]: w for w in r["wells"]}
    out: Checks = [(f"wells == {sorted(expect)}", set(ws) == set(expect))]
    if "WELL-0164" in ws:
        out.append(("0164 and 0175 'no recorded reason'",
                    ws["WELL-0164"]["reason_summary"] == "no recorded reason" and ws["WELL-0175"]["reason_summary"] == "no recorded reason"))
    out.append(("0170 -> MNT-0170-031, 0172 -> MNT-0172-028",
                ws["WELL-0170"]["reasons"][0]["id"] == "MNT-0170-031" and ws["WELL-0172"]["reasons"][0]["id"] == "MNT-0172-028"))
    return out


def q3(c: Ctx) -> Checks:
    r = c.tool("inspection_history", {"well_id": "WELL-0001", "last_n": 13})
    return [("13 readings from 2026-07-23", len(r["points"]) == 13 and r["points"][0]["date"] == "2026-07-23"),
            ("not producing 8 and 15 Oct", r["not_producing_dates"] == ["2026-10-08", "2026-10-15"])]


def q4(c: Ctx) -> Checks:
    s = c.tool("spills", {"scope": "WELL-0001", "period": "last_4_years"})
    return [("6 spills", s["count"] == 6), ("15.1 bbl", s["total_volume_bbl"] == 15.1),
            ("INS-0001-0070 excluded", all(x.get("inspection_id") != "INS-0001-0070" for x in s["spills"]))]


def q5(c: Ctx) -> Checks:
    return [("21 days", c.tool("downtime", {"scope": "WELL-0001", "period": "2025"})["total_days"] == 21)]


def q6(c: Ctx) -> Checks:
    d = c.tool("downtime", {"scope": "Northeast Block 7", "period": "last_4_months", "top_n": 3})
    got = {w["asset_id"]: w["downtime_days"] for w in d["wells"]}
    return [("0121=49, 0138=42, 0107=42", got == {"WELL-0121": 49, "WELL-0138": 42, "WELL-0107": 42})]


def q7(c: Ctx) -> Checks:
    r = c.tool("spills", {"scope": "Northeast Block 8", "period": "last_90_days"})
    return [("4 wells 0151/0165/0175/0186", {s["asset_id"] for s in r["spills"]} == {"WELL-0151", "WELL-0165", "WELL-0175", "WELL-0186"}),
            ("coordinates present", all(s["lat"] and s["lon"] for s in r["spills"]))]


def q8(c: Ctx) -> Checks:
    h = c.tool("spills", {"scope": "all", "period": "2026", "group_by": "area_month"})["heatmap"]
    tot = dict(zip(h["rows"], h["row_totals"]))
    return [("area totals 35/43/37/26", tot == {"Southwest Block 23": 35, "Southwest Block 24": 43, "Northeast Block 7": 37, "Northeast Block 8": 26}),
            ("Jan-Oct columns", len(h["columns"]) == 10)]


def q9(c: Ctx) -> Checks:
    r = c.tool("compliance_check", {"well_id": "WELL-0001", "period": "last_12_months"})
    return [("breaches SR-01/02/03 + RC-04", sorted(b["rule"] for b in r["breaches"]) == ["RC-04", "SR-01", "SR-02", "SR-03"]),
            ("open fine USD 5000", r["open_fines_usd"] == 5000),
            ("flowline RCA pending", any(a["rule"] == "RCA" and a["equipment_kind"] == "flowline" for a in r["open_actions"])),
            ("RC-02 met", any(m["rule"] == "RC-02" for m in r["rules_met"]))]


def q10(c: Ctx) -> Checks:
    out = {}
    for (w,) in c.conn.execute("SELECT asset_id FROM wells WHERE area_id='SW-24'").fetchall():
        for b in c.tool("compliance_check", {"well_id": w, "period": "last_90_days"})["breaches"]:
            if b["rule"] == "SR-02":
                out[w] = b["containment_hours"]
    return [("WELL-0082 and WELL-0100 only", set(out) == {"WELL-0082", "WELL-0100"}),
            ("~7.1 h and ~8.1 h", approx(out.get("WELL-0082"), 7.1, 0.1) and approx(out.get("WELL-0100"), 8.1, 0.1))]


MICHAEL = "Michael Turner"


def q11(c: Ctx) -> Checks:
    conv = memory.start_conversation(c.conn, c.uid(MICHAEL), "Q6")
    c.tool("downtime", {"scope": "Northeast Block 7", "period": "last_4_months", "top_n": 3}, MICHAEL, conv)
    memory.advance_day(c.conn, 1)
    rec = c.tool("recall", {"when": "yesterday"}, MICHAEL)
    wells = rec.get("wells_discussed", [])
    cost = {w["asset_id"]: w["total_usd"] for w in c.tool("repair_costs", {"scope": "all", "period": "2026"})["wells"]}
    return [("recall finds the 3 wells", rec["found"] and {"WELL-0121", "WELL-0138", "WELL-0107"} <= set(wells)),
            ("costs 25794 / 46942 / 5176", (cost["WELL-0121"], cost["WELL-0138"], cost["WELL-0107"]) == (25794, 46942, 5176))]


def q12(c: Ctx) -> Checks:
    r = c.tool("record_correction", {"inspection_id": "INS-0164-0205", "field": "wellhead_temp_c", "new_value": 41.0,
                                     "reason": "handheld thermal gun faulty; re-measured 41.0 C"}, MICHAEL)
    st = c.tool("well_status", {"well_id": "WELL-0164"})
    raw = c.conn.execute("SELECT wellhead_temp_c FROM inspections WHERE inspection_id='INS-0164-0205'").fetchone()[0]
    return [("correction accepted", "error" not in r),
            ("conflict with 'Pump jack not moving' flagged", bool(r.get("needs_confirmation")) and any("Pump jack" in str(x) for x in r.get("conflicts", []))),
            ("now producing, delta ~21.1", st["producing"] is True and approx(st["latest_inspection"]["delta_c"], 21.1)),
            ("original 24.0 kept", raw == 24.0),
            ("author and reason recorded", MICHAEL in json.dumps(st.get("corrections")) and "thermal" in json.dumps(st.get("corrections")))] + \
        q2(c, expect=("WELL-0170", "WELL-0172", "WELL-0175"))


def q13(c: Ctx) -> Checks:
    r = c.tool("record_correction", {"inspection_id": "INS-0151-0201", "field": "spill_est_volume_bbl", "new_value": 5.4,
                                     "reason": "field crew measured 5.4 bbl"}, MICHAEL)
    txt = json.dumps(r.get("downstream"))
    return [("correction accepted", "error" not in r),
            ("SR-03 due 2026-11-23", "2026-11-23" in txt),
            ("report MNT-0151-025 reclassification flagged", "MNT-0151-025" in txt),
            ("SR-01 no written notice flagged", "SR-01" in txt)]


def q14(c: Ctx) -> Checks:
    args = {"scope": "Northeast Block 7", "equipment_kind": "pump_jack"}
    before = set(c.tool("root_cause_triggers", args)["wells"])
    r = c.tool("remember_fact", {"scope_type": "well_type", "scope_id": "T350", "text": "for all T350 wells root-cause analysis after 2 repairs, not 3",
                                 "rule_key": "rca_trigger_corrective_count", "rule_value": 2}, MICHAEL)
    after_r = c.tool("root_cause_triggers", args)
    after = set(after_r["wells"])
    return [("before: 0108, 0121, 0138", before == {"WELL-0108", "WELL-0121", "WELL-0138"}),
            ("rule stored", "error" not in r),
            ("after: 0108, 0120, 0121, 0138", after == {"WELL-0108", "WELL-0120", "WELL-0121", "WELL-0138"}),
            ("WELL-0102 (T200) not added", "WELL-0102" not in after),
            ("answer attributes the user's rule", MICHAEL in json.dumps(after_r))]


def q15(c: Ctx) -> Checks:
    st = c.tool("well_status", {"well_id": "WELL-0164"}, "Jessica Reed")
    sp = c.tool("spills", {"scope": "Northeast Block 8", "period": "last_90_days"}, "Laura Simmons")
    rep = [s for s in sp["spills"] if s["needs_regulator_report"]]
    return [("Jessica sees 0164 producing + correction", st["producing"] is True and bool(st["corrections"])),
            ("Laura: only WELL-0151 needs a report", [s["asset_id"] for s in rep] == ["WELL-0151"]),
            ("no notice on record (overdue)", bool(rep) and rep[0]["notice_on_record"] is None)]


def q16(c: Ctx) -> Checks:
    r = c.tool("remember_fact", {"scope_type": "well", "scope_id": "WELL-0007", "text": "shut in from 17 Oct for a 10-day planned workover",
                                 "state": "shut_in", "since": "2026-10-17", "duration_days": 10}, "Jake Morales")
    sd = c.tool("well_status", {"well_id": "WELL-0007"}, MICHAEL)["user_shutdowns"]
    jess = c.tool("recall", {"when": "yesterday"}, "Jessica Reed")
    return [("fact stored", "error" not in r),
            ("Michael sees it, attributed to Jake, until 27 Oct", bool(sd) and sd[0]["author"] == "Jake Morales" and sd[0]["until"] == "2026-10-27"),
            ("Jessica: no such conversation", jess["found"] is False),
            ("no leakage of Michael's wells", "WELL-0121" not in json.dumps(jess))]


def q17(c: Ctx) -> Checks:
    a = c.tool("remember_fact", {"scope_type": "well", "scope_id": "WELL-0175", "text": "shut in for planned work since 14 Oct",
                                 "state": "shut_in", "since": "2026-10-14"}, MICHAEL)
    b = c.tool("remember_fact", {"scope_type": "well", "scope_id": "WELL-0175", "text": "running fine", "state": "producing"}, "Tyler Hughes")
    d = c.tool("remember_fact", {"scope_type": "well", "scope_id": "WELL-0001", "text": "back online since 14 Oct",
                                 "state": "producing", "since": "2026-10-14"}, "Jake Morales")
    return [("Michael's claim consistent", not a["needs_confirmation"]),
            ("Tyler's claim unconfirmed with conflicts", b["status"] == "unconfirmed" and b["needs_confirmation"] and bool(b["conflicts"])),
            ("conflict cites the inspection data", "INS-0175" in json.dumps(b["conflicts"])),
            ("Jake's claim unconfirmed, cites CTX-0001-003", d["status"] == "unconfirmed" and "CTX-0001-003" in json.dumps(d["conflicts"]))]


def q18(c: Ctx) -> Checks:
    r = c.tool("explain_spill", {"well_id": "WELL-0101", "date_": "2026-08-20"}, "Laura Simmons")
    return [("INS-0101-0197 found", bool(r.get("found")) and r["inspection_id"] == "INS-0101-0197"),
            ("detection stands (yes)", r.get("detection_stands") is True),
            ("both notes shown", {"CTX-0101-002", "CTX-0101-003"} <= {n["note_id"] for n in r.get("notes", [])}),
            ("no formal correction", r.get("formal_corrections") == [])]


TOOL_CHECKS = {1: q1, 2: q2, 3: q3, 4: q4, 5: q5, 6: q6, 7: q7, 8: q8, 9: q9, 10: q10, 11: q11, 12: q12, 13: q13,
               14: q14, 15: q15, 16: q16, 17: q17, 18: q18}


# ---------------------------------------------------------------- end-to-end (LLM) scenarios
# `has` is a list of requirements; each is a string or a list of alternatives (any one must appear in the
# answer text or the rendered visual). Numbers are compared without thousands separators.
def T(user, msg, visual=None, has=(), lacks=(), tools=(), new_chat=False, next_day=False, table_lacks=()):
    return dict(user=user, msg=msg, visual=visual, has=[list(h) if isinstance(h, (list, tuple)) else [h] for h in has],
                lacks=list(lacks), tools=list(tools), new_chat=new_chat, next_day=next_day, table_lacks=list(table_lacks))


Q2_MSG = "Which wells in Northeast Block 8 are not producing oil right now, and why? Show them in a table."
Q6_MSG = "Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months? Show a bar chart."
Q14_MSG = "Which wells in Northeast Block 7 need a root-cause analysis on their pump jack?"
LLM_SCENARIOS = {
    1: [T("Ryan Brooks", "Is WELL-0001 producing oil right now? When was it last inspected, and by whom? Show me its status card.",
          "status_card", [["not producing", "no,", "is not"], "INS-0001-0203", "Ryan Brooks", ["CTX-0001-003", "shut-in", "shut in"]], tools=["well_status"])],
    2: [T("Emily Carter", Q2_MSG, "table", ["WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175", ["no recorded reason"]], tools=["list_wells_not_producing"])],
    3: [T("Emily Carter", "Show a line chart of WELL-0001's wellhead temperature against air temperature for the last 13 weeks, and mark when it was not producing oil.",
          "line_chart", [["8 oct", "2026-10-08"], ["15 oct", "2026-10-15"]], tools=["inspection_history"])],
    4: [T("Emily Carter", "How many oil spills has WELL-0001 had in the last 4 years, and how much oil in total? List them in a table.",
          "table", ["6", "15.1"], tools=["spills"])],
    5: [T("Emily Carter", "How many days was WELL-0001 down in 2025?", None, ["21"], tools=["downtime"])],
    6: [T("Emily Carter", Q6_MSG, "bar_chart", ["WELL-0121", "49", "WELL-0138", "WELL-0107", "42"], tools=["downtime"])],
    7: [T("Emily Carter", "Show on a map every well in Northeast Block 8 with a real spill in the last 90 days.", "map",
          ["WELL-0151", "WELL-0165", "WELL-0175", "WELL-0186"], tools=["spills"])],
    8: [T("Emily Carter", "Show a heatmap of 2026 spills by geographic area and month.", "heatmap", ["43", "35", "37", "26"], tools=["spills"])],
    9: [T("Emily Carter", "Which rules has WELL-0001 broken in the last 12 months, and what is still open?", None,
          ["SR-01", "SR-02", "SR-03", "RC-04", ["5000", "5 000"]], tools=["compliance_check"])],
    10: [T("Emily Carter", "In Southwest Block 24, which spills in the last 90 days were not contained within the SOP time limit?",
           "table", ["WELL-0082", "WELL-0100", "7.1", "8.1"], tools=["compliance_check", "sql_query"])],
    11: [T(MICHAEL, Q6_MSG, "bar_chart", ["WELL-0121"], tools=["downtime"]),
         T(MICHAEL, "For the three wells we discussed yesterday, what have repairs cost this year?", None,
           ["25794", "46942", "5176"], tools=["recall", "repair_costs"], new_chat=True, next_day=True)],
    12: [T(MICHAEL, "The 15 Oct reading for WELL-0164 is wrong. The handheld thermal gun used in that inspection was faulty; the field crew re-measured the wellhead at 41.0 °C.",
           None, ["41.0", "24.0", "producing", "pump jack", "confirm"], tools=["record_correction"]),
          T(MICHAEL, Q2_MSG, "table", ["WELL-0170", "WELL-0172", "WELL-0175"], tools=["list_wells_not_producing"], new_chat=True, table_lacks=["WELL-0164"])],
    13: [T(MICHAEL, "The AI estimated the 24 Sep spill at WELL-0151 as 4.5 bbl. The field crew measured 5.4 bbl.", None,
           ["5.4", "reportable", ["no written notice", "none is recorded", "no notice", "not recorded"], "2026-11-23"], tools=["record_correction"])],
    14: [T(MICHAEL, Q14_MSG, None, ["WELL-0108", "WELL-0121", "WELL-0138"], lacks=["WELL-0120"], tools=["root_cause_triggers"]),
         T(MICHAEL, "Note for all T350 wells: our team now wants a pump jack root-cause analysis after 2 repairs, not 3.", None, [], tools=["remember_fact"]),
         T(MICHAEL, Q14_MSG, None, ["WELL-0108", "WELL-0120", "WELL-0121", "WELL-0138", ["your rule", "michael turner", "team rule", "user rule", "not the manual"]],
           lacks=["WELL-0102"], tools=["root_cause_triggers"], new_chat=True)],
    15: [T("Jessica Reed", "Is WELL-0164 producing oil right now?", None, [["yes", "is producing"], "41.0", "Michael Turner", "pump jack"], tools=["well_status"]),
         T("Laura Simmons", "Which spills in Northeast Block 8 in the last 90 days needed a report to the regulator?", None,
           ["WELL-0151", ["overdue", "no written notice", "no notice", "none is recorded", "not recorded"]], lacks=["WELL-0165"], tools=["spills"])],
    16: [T("Jake Morales", "FYI for everyone: WELL-0007 will be shut in from 17 Oct for a 10-day planned workover.", None, [], tools=["remember_fact"]),
         T(MICHAEL, "Is any downtime planned for WELL-0007?", None, [["17 oct", "2026-10-17"], "Jake Morales", ["10 day", "10-day", "27 oct"]], tools=["well_status"], new_chat=True),
         T("Jessica Reed", "For the three wells we discussed yesterday, what have repairs cost this year?", None,
           [["no such", "no earlier", "no previous", "don't have", "do not have", "no record", "not found"]], lacks=["25794", "46942", "WELL-0121"], tools=["recall"])],
    17: [T(MICHAEL, "WELL-0175 has been shut in for planned work since 14 Oct.", None, [], tools=["remember_fact"]),
         T("Tyler Hughes", "WELL-0175 is running fine.", None, ["0.4", "unconfirmed", "confirm", "Michael Turner"], tools=["remember_fact"]),
         T("Jake Morales", "WELL-0001 is back online since 14 Oct.", None, ["not producing", "unconfirmed", "CTX-0001-003"], tools=["remember_fact"])],
    18: [T("Laura Simmons", "Was the 20 Aug spill at WELL-0101 real oil?", None,
           [["yes", "real oil", "crude"], "CTX-0101-002", "CTX-0101-003", "Chris Dalton", "Ryan Brooks"], tools=["explain_spill"])],
}


def _norm(s: str) -> str:
    return s.lower().replace(",", "").replace("\u00a0", " ")


def run_llm_turn(c: Ctx, provider, turn: dict, state: dict) -> Checks:
    from app.agent import run_turn
    if turn["next_day"]:
        memory.advance_day(c.conn, 1)
    if turn["new_chat"] or state["user"] != turn["user"]:
        state["conv"] = None
    state["user"] = turn["user"]
    text, visual, tools, err = "", None, [], None
    for ev in run_turn(c.conn, provider, c.uid(turn["user"]), turn["msg"], state["conv"]):
        if ev["type"] == "conversation":
            state["conv"] = ev["id"]
        elif ev["type"] == "text":
            text += ev["text"]
        elif ev["type"] == "tool_call":
            tools.append(ev["name"])
        elif ev["type"] == "visual":
            visual = ev["spec"]
        elif ev["type"] == "error":
            err = ev["message"]
    t = _norm(text)
    vis_blob = _norm(json.dumps(visual or {}, default=str))
    out: Checks = [(f"[{turn['user']}] no error" + (f": {err}" if err else ""), err is None)]
    if turn["visual"]:
        out.append((f"visual == {turn['visual']}", (visual or {}).get("type") == turn["visual"]))
    if turn["tools"]:
        out.append((f"tool called: {'/'.join(turn['tools'])}", any(x in tools for x in turn["tools"])))
    for alts in turn["has"]:
        out.append((f"mentions {alts[0]!r}" + (f" (+{len(alts) - 1} alt)" if len(alts) > 1 else ""),
                    any(_norm(a) in t or _norm(a) in vis_blob for a in alts)))
    for bad in turn["lacks"]:
        out.append((f"does not mention {bad!r}", _norm(bad) not in t))
    if turn["table_lacks"]:
        out += [(f"visual lacks {b!r}", b not in json.dumps((visual or {}).get("data"))) for b in turn["table_lacks"]]
    return out


# ---------------------------------------------------------------- runner
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", action="store_true", help="run end to end through the agent and the configured LLM")
    ap.add_argument("--only", type=int, nargs="*", help="question numbers to run")
    ap.add_argument("--data-dir", action="append", help="dataset folder (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every check, not only failures")
    ap.add_argument("--set", choices=["sample", "final"], default="sample",
                    help="'final' = the 11 final questions on test-data/dataset (today = 2027-01-15)")
    a = ap.parse_args(argv)
    tool_checks, scenarios = TOOL_CHECKS, LLM_SCENARIOS
    if a.set == "final":
        from tests.final_questions import FINAL_SCENARIOS, FINAL_TOOL_CHECKS
        tool_checks, scenarios = FINAL_TOOL_CHECKS, FINAL_SCENARIOS
        os.environ.setdefault("DATA_DIR", "test-data/dataset")
        if not a.data_dir and "test-data" not in os.environ.get("DATA_DIR", ""):
            os.environ["DATA_DIR"] = "test-data/dataset"
        os.environ["TODAY"] = "2027-01-15"
        os.environ["RULES_PATH"] = "rules/rules_test.json"
    if a.data_dir:
        os.environ["DATA_DIR"] = os.pathsep.join(a.data_dir)

    tmp = Path(tempfile.mkdtemp(prefix="eval_")) / "eval.db"
    ingest.ingest(config.data_dirs(), reset=True, db_file=tmp)
    conn = db.connect(tmp)
    memory.reset(conn)
    ctx = Ctx(conn)
    provider = None
    if a.llm:
        from app.llm import get_provider, llm_configured
        if not llm_configured():
            print("No LLM configured (set GOOGLE_API_KEY or LLM_PROVIDER); cannot run --llm.")
            return 2
        provider = get_provider()

    rows, failures = [], []
    for q in sorted(a.only or tool_checks):
        t0 = time.time()
        try:
            if a.llm:
                state, checks = {"conv": None, "user": None}, []
                for turn in scenarios[q]:
                    checks += run_llm_turn(ctx, provider, turn, state)
            else:
                checks = tool_checks[q](ctx)
        except Exception as e:  # a crashing question must not hide the others
            checks = [(f"crashed: {type(e).__name__}: {e}", False)]
        ok = all(v for _, v in checks)
        rows.append((q, "PASS" if ok else "FAIL", sum(bool(v) for _, v in checks), len(checks), time.time() - t0))
        failures += [(q, label) for label, v in checks if not v]
        if a.verbose:
            for label, v in checks:
                print(f"   Q{q:<2} {'ok  ' if v else 'FAIL'} {label}")

    print(f"\nMode: {'LLM end-to-end' if a.llm else 'tool-level'}   Data: {', '.join(str(p) for p in config.data_dirs())}\n")
    print(f"{'Q':<5}{'Result':<8}{'Checks':<10}{'Time':>6}")
    print("-" * 30)
    for q, res, n, tot, dt in rows:
        print(f"Q{q:<4}{res:<8}{f'{n}/{tot}':<10}{dt:>5.1f}s")
    print("-" * 30)
    print(f"{sum(1 for r in rows if r[1] == 'PASS')}/{len(rows)} questions passed")
    if failures:
        print("\nFailed checks:")
        for q, label in failures:
            print(f"  Q{q}: {label}")
    conn.close()
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
