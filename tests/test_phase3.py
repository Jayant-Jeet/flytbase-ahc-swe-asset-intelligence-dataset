"""Phase 3: shared memory, corrections, rules, private memory and conflict handling (Q11-Q18)."""
import pytest

from app import config, db, ingest, memory
from app.tools import call_tool


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    f = tmp_path_factory.mktemp("db3") / "t.db"
    ingest.ingest(config.data_dirs(), reset=True, db_file=f)
    c = db.connect(f)
    yield c
    c.close()


@pytest.fixture()
def conn(base):
    memory.reset(base)
    yield base
    memory.reset(base)


def uid(conn, name):
    return conn.execute("SELECT user_id FROM users WHERE name=?", (name,)).fetchone()[0]


def tool(conn, name, args, who=None, conv=None):
    return call_tool(conn, name, args, user_id=uid(conn, who) if who else None, conv_id=conv)


def not_producing(conn):
    r = tool(conn, "list_wells_not_producing", {"scope": "Northeast Block 8"})
    return {w["well_id"] for w in r["wells"]}


def test_q12_correction_flips_status_and_flags_conflict(conn):
    r = tool(conn, "record_correction", {"inspection_id": "INS-0164-0205", "field": "wellhead_temp_c", "new_value": 41.0,
                                         "reason": "gauge misread"}, "Michael Turner")
    assert "error" not in r, r
    assert r["needs_confirmation"] and any("Pump jack" in str(c) for c in r["conflicts"])
    st = tool(conn, "well_status", {"well_id": "WELL-0164"})
    assert st["producing"] is True
    assert st["latest_inspection"]["delta_c"] == pytest.approx(21.1, abs=0.05)
    raw = conn.execute("SELECT wellhead_temp_c FROM inspections WHERE inspection_id='INS-0164-0205'").fetchone()[0]
    assert raw == 24.0
    assert not_producing(conn) == {"WELL-0170", "WELL-0172", "WELL-0175"}


def test_q13_volume_correction_reportability(conn):
    r = tool(conn, "record_correction", {"inspection_id": "INS-0151-0201", "field": "spill_est_volume_bbl",
                                         "new_value": 5.4, "reason": "re-measured"}, "Michael Turner")
    assert "error" not in r, r
    text = str(r["downstream"])
    assert "2026-11-23" in text and "MNT-0151-025" in text


def test_q14_t350_rule_only_affects_t350(conn):
    before = tool(conn, "root_cause_triggers", {"scope": "Northeast Block 7", "equipment_kind": "pump_jack"})
    assert set(before["wells"]) == {"WELL-0108", "WELL-0121", "WELL-0138"}
    r = tool(conn, "remember_fact", {"scope_type": "well_type", "scope_id": "T350",
                                     "text": "for all T350 wells root-cause analysis after 2 repairs",
                                     "rule_key": "rca_trigger_corrective_count", "rule_value": 2}, "Michael Turner")
    assert "error" not in r, r
    after = tool(conn, "root_cause_triggers", {"scope": "Northeast Block 7", "equipment_kind": "pump_jack"})
    ids = set(after["wells"])
    assert ids == {"WELL-0108", "WELL-0120", "WELL-0121", "WELL-0138"}
    assert "Michael Turner" in str(after)
    assert tool(conn, "get_rules", {"well_type": "T200"})["rules"]


def test_q15_other_user_sees_correction_and_spill_report(conn):
    tool(conn, "record_correction", {"inspection_id": "INS-0164-0205", "field": "wellhead_temp_c", "new_value": 41.0,
                                     "reason": "gauge misread"}, "Michael Turner")
    tool(conn, "record_correction", {"inspection_id": "INS-0151-0201", "field": "spill_est_volume_bbl", "new_value": 5.4,
                                     "reason": "re-measured"}, "Michael Turner")
    st = tool(conn, "well_status", {"well_id": "WELL-0164"})
    assert st["producing"] is True and st["corrections"]
    sp = tool(conn, "spills", {"scope": "Northeast Block 8", "period": "last_90_days"})
    rep = [s for s in sp["spills"] if s["needs_regulator_report"]]
    assert [s["asset_id"] for s in rep] == ["WELL-0151"]
    assert rep[0]["notice_on_record"] is None


def test_q16_shared_fact_and_private_memory(conn):
    r = tool(conn, "remember_fact", {"scope_type": "well", "scope_id": "WELL-0007", "text": "shut in from 17 Oct for 10 days",
                                     "state": "shut_in", "since": "2026-10-17", "duration_days": 10}, "Jake Morales")
    assert "error" not in r, r
    st = tool(conn, "well_status", {"well_id": "WELL-0007"}, "Michael Turner")
    sd = st["user_shutdowns"]
    assert sd and sd[0]["author"] == "Jake Morales" and sd[0]["until"] == "2026-10-27"

    # Michael had a conversation "yesterday"; Jessica has none.
    c = memory.start_conversation(conn, uid(conn, "Michael Turner"))
    tool(conn, "downtime", {"scope": "Northeast Block 7", "period": "last_4_months", "top_n": 3}, "Michael Turner", c)
    memory.advance_day(conn, 1)
    mine = tool(conn, "recall", {"when": "yesterday"}, "Michael Turner")
    assert mine["found"] and {"WELL-0121", "WELL-0138", "WELL-0107"} <= set(mine["wells_discussed"])
    theirs = tool(conn, "recall", {"when": "yesterday"}, "Jessica Reed")
    assert theirs["found"] is False and "WELL-0121" not in str(theirs)


def test_q17_conflicting_claims(conn):
    a = tool(conn, "remember_fact", {"scope_type": "well", "scope_id": "WELL-0175", "text": "shut in since 14 Oct",
                                     "state": "shut_in", "since": "2026-10-14"}, "Michael Turner")
    assert not a["needs_confirmation"]
    b = tool(conn, "remember_fact", {"scope_type": "well", "scope_id": "WELL-0175", "text": "running fine",
                                     "state": "producing"}, "Tyler Hughes")
    assert b["needs_confirmation"] and b["conflicts"] and b["status"] == "unconfirmed"
    c = tool(conn, "remember_fact", {"scope_type": "well", "scope_id": "WELL-0001", "text": "back online since 14 Oct",
                                     "state": "producing", "since": "2026-10-14"}, "Jake Morales")
    assert c["needs_confirmation"] and c["status"] == "unconfirmed"
    assert len(c["conflicts"]) >= 1


def test_confirm_flow(conn):
    b = tool(conn, "remember_fact", {"scope_type": "well", "scope_id": "WELL-0175", "text": "running fine",
                                     "state": "producing"}, "Tyler Hughes")
    r = tool(conn, "confirm_fact", {"fact_id": b["fact_id"], "decision": "withdraw"}, "Tyler Hughes")
    assert r["status"] == "disputed"
    st = tool(conn, "well_status", {"well_id": "WELL-0175"})
    assert all(f["fact_id"] != b["fact_id"] for f in st["shared_facts"])


def test_q18_explain_spill(conn):
    r = tool(conn, "explain_spill", {"well_id": "WELL-0101", "date_": "2026-08-20"})
    assert r["found"], r
    assert r["inspection_id"] == "INS-0101-0197" and r["detection_stands"] is True
    assert {"CTX-0101-002", "CTX-0101-003"} <= {n["note_id"] for n in r["notes"]}
    assert r["formal_corrections"] == []


def test_reingest_preserves_memory(conn, tmp_path):
    tool(conn, "remember_fact", {"scope_type": "well", "scope_id": "WELL-0007", "text": "shut in", "state": "shut_in",
                                 "since": "2026-10-17"}, "Jake Morales")
    f = tmp_path / "x.db"
    ingest.ingest(config.data_dirs(), reset=True, db_file=f)
    c2 = db.connect(f)
    memory.add_fact(c2, "well", "WELL-0007", "x", {}, "U-102", "confirmed")
    c2.commit()
    c2.close()
    ingest.ingest(config.data_dirs(), reset=True, db_file=f)
    c3 = db.connect(f)
    assert c3.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
    c3.close()
