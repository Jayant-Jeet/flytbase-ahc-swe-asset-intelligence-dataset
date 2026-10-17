"""Phase 2 tool checks against the sample dataset (expected values from 03-evaluation-queries.md)."""
import pytest

from app import config, db, ingest
from app.tools import call_tool


@pytest.fixture(scope="session")
def conn(tmp_path_factory):
    f = tmp_path_factory.mktemp("db") / "t.db"
    ingest.ingest(config.data_dirs(), reset=True, db_file=f)
    c = db.connect(f)
    yield c
    c.close()


def test_q1_well_status(conn):
    r = call_tool(conn, "well_status", {"well_id": "WELL-0001"})
    li = r["latest_inspection"]
    assert r["producing"] is False and li["inspection_id"] == "INS-0001-0203" and li["inspector_name"] == "Ryan Brooks"
    assert li["delta_c"] == 3.2 and r["recorded_status"] == "active" and r["recorded_status_out_of_date"]
    assert [n["note_id"] for n in r["planned_shutdowns"]] == ["CTX-0001-003"]


def test_q2_ne8_not_producing(conn):
    r = call_tool(conn, "list_wells_not_producing", {"scope": "Northeast Block 8"})
    ws = {w["well_id"]: w for w in r["wells"]}
    assert set(ws) == {"WELL-0164", "WELL-0170", "WELL-0172", "WELL-0175"}
    assert ws["WELL-0164"]["reason_summary"] == "no recorded reason" and ws["WELL-0175"]["reason_summary"] == "no recorded reason"
    assert ws["WELL-0170"]["reasons"][0]["id"] == "MNT-0170-031" and ws["WELL-0172"]["reasons"][0]["id"] == "MNT-0172-028"


def test_q3_history(conn):
    r = call_tool(conn, "inspection_history", {"well_id": "WELL-0001", "last_n": 13})
    assert len(r["points"]) == 13 and r["points"][0]["date"] == "2026-07-23"
    assert r["not_producing_dates"] == ["2026-10-08", "2026-10-15"]


def test_q4_q5_q6(conn):
    s = call_tool(conn, "spills", {"scope": "WELL-0001", "period": "last_4_years"})
    assert s["count"] == 6 and s["total_volume_bbl"] == 15.1
    assert call_tool(conn, "downtime", {"scope": "WELL-0001", "period": "2025"})["total_days"] == 21
    d = call_tool(conn, "downtime", {"scope": "Northeast Block 7", "period": "last_4_months", "top_n": 3})
    assert [(w["asset_id"], w["downtime_days"]) for w in d["wells"]][0] == ("WELL-0121", 49)
    assert {w["asset_id"] for w in d["wells"]} == {"WELL-0121", "WELL-0138", "WELL-0107"}


def test_q7_map(conn):
    r = call_tool(conn, "spills", {"scope": "Northeast Block 8", "period": "last_90_days"})
    assert {s["asset_id"] for s in r["spills"]} == {"WELL-0151", "WELL-0165", "WELL-0175", "WELL-0186"}
    assert all(s["lat"] for s in r["spills"])


def test_q8_heatmap(conn):
    h = call_tool(conn, "spills", {"scope": "all", "period": "2026", "group_by": "area_month"})["heatmap"]
    assert dict(zip(h["rows"], h["row_totals"])) == {"Southwest Block 23": 35, "Southwest Block 24": 43,
                                                     "Northeast Block 7": 37, "Northeast Block 8": 26}
    assert h["columns"][0] == "2026-01" and len(h["columns"]) == 10
    row = dict(zip(h["rows"], h["values"]))["Southwest Block 23"]
    assert row == [4, 3, 3, 6, 3, 4, 4, 3, 3, 2]


def test_q9_compliance(conn):
    r = call_tool(conn, "compliance_check", {"well_id": "WELL-0001", "period": "last_12_months"})
    assert sorted(b["rule"] for b in r["breaches"]) == ["RC-04", "SR-01", "SR-02", "SR-03"]
    assert r["open_fines_usd"] == 5000
    assert any(a["rule"] == "RCA" and a["equipment_kind"] == "flowline" for a in r["open_actions"])
    assert any(m["rule"] == "RC-02" for m in r["rules_met"])
    sr03 = next(b for b in r["breaches"] if b["rule"] == "SR-03")
    assert sr03["verification_due"] == "2026-08-10"


def test_q10_uncontained(conn):
    out = {}
    for w in [r[0] for r in conn.execute("SELECT asset_id FROM wells WHERE area_id='SW-24'")]:
        c = call_tool(conn, "compliance_check", {"well_id": w, "period": "last_90_days"})
        for b in c["breaches"]:
            if b["rule"] == "SR-02":
                out[w] = b["containment_hours"]
    assert set(out) == {"WELL-0082", "WELL-0100"}


def test_q11_costs(conn):
    r = call_tool(conn, "repair_costs", {"scope": "all", "period": "2026"})
    c = {w["asset_id"]: w["total_usd"] for w in r["wells"]}
    assert (c["WELL-0121"], c["WELL-0138"], c["WELL-0107"]) == (25794, 46942, 5176)


def test_sql_readonly(conn):
    assert call_tool(conn, "sql_query", {"query": "SELECT COUNT(*) FROM wells"})["rows"] == [[200]]
    assert "error" in call_tool(conn, "sql_query", {"query": "DELETE FROM wells"})
    assert "error" in call_tool(conn, "sql_query", {"query": "SELECT 1; DROP TABLE wells"})
    assert call_tool(conn, "sql_query", {"query": "SELECT COUNT(*) FROM wells"})["rows"] == [[200]]


def test_search_and_rules(conn):
    assert any(h["chunk_id"].endswith("SR-02") for h in call_tool(conn, "search_docs", {"query": "containment time limit", "doc_type": "sop"})["results"])
    assert any(r["rule_key"] == "producing_min_delta_c" for r in call_tool(conn, "get_rules", {"well_type": "T350"})["rules"])
    assert "error" in call_tool(conn, "downtime", {"scope": "WELL-0001"})
    assert "error" in call_tool(conn, "nope", {})
