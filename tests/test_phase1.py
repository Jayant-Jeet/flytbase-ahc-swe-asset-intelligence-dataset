"""Phase 1 checks against the sample dataset (expected values from 03-evaluation-queries.md)."""
from datetime import date

import pytest

from app import config, db, ingest, metrics
from pathlib import Path


@pytest.fixture(scope="session")
def conn(tmp_path_factory):
    f = tmp_path_factory.mktemp("db") / "t.db"
    ingest.ingest(config.data_dirs(), reset=True, db_file=f)
    c = db.connect(f)
    yield c
    c.close()


def test_rules_from_table(conn):
    g = lambda k, t: conn.execute("SELECT value_num FROM rules WHERE rule_key=? AND scope_id=?", (k, t)).fetchone()[0]
    assert g("producing_min_delta_c", "T200") == 15 and g("producing_min_delta_c", "T350") == 20
    assert g("sr03_verify_days", "default") == 60
    assert g("sr02_containment_hours", "default") == 4


def test_q1_well_status(conn):
    r = conn.execute("SELECT * FROM inspection_effective WHERE asset_id='WELL-0001' "
                     "ORDER BY inspected_at DESC LIMIT 1").fetchone()
    assert r["inspection_id"] == "INS-0001-0203" and r["inspected_date"] == "2026-10-15"
    assert r["producing"] == 0 and r["delta_c"] == 3.2 and r["inspector_user_id"]


def test_q4_spills_with_overlay(conn):
    s = metrics.spills(conn, "WELL-0001", "2022-01-01", "2026-10-17")
    assert s["count"] == 6 and s["total_volume_bbl"] == 15.1
    assert [e["inspection_id"] for e in s["excluded_by_correction"]] == ["INS-0001-0070"]


def test_q5_downtime_2025(conn):
    assert metrics.downtime(conn, "WELL-0001", "2025-01-01", "2025-12-31")["total_days"] == 21


@pytest.mark.parametrize("today", ["2026-10-16", "2026-10-17"])
def test_q6_ne7(conn, today):
    start, end = metrics.last_n_months(4, date.fromisoformat(today))
    top = [(w["asset_id"], w["downtime_days"]) for w in metrics.downtime(conn, "Northeast Block 7", start, end)["wells"][:3]]
    assert dict(top) == {"WELL-0121": 49, "WELL-0138": 42, "WELL-0107": 42}


def test_q8_area_totals(conn):
    tot = {}
    for a, n in conn.execute("SELECT area_id, name FROM areas"):
        tot[n] = metrics.spills(conn, a, "2026-01-01", "2026-10-17")["count"]
    assert tot == {"Southwest Block 23": 35, "Southwest Block 24": 43, "Northeast Block 7": 37, "Northeast Block 8": 26}


def test_q10_uncontained(conn):
    rows = conn.execute(
        """SELECT m.asset_id, f.containment_hours FROM report_facts f JOIN maintenance m USING(maintenance_id)
           JOIN wells w ON w.asset_id=m.asset_id
           WHERE w.area_id='SW-24' AND m.started_date BETWEEN '2026-07-19' AND '2026-10-17'
             AND f.containment_hours > (SELECT value_num FROM rules WHERE rule_key='sr02_containment_hours')""").fetchall()
    got = {r[0]: r[1] for r in rows}
    assert set(got) == {"WELL-0082", "WELL-0100"}
    assert abs(got["WELL-0082"] - 7.1) < 0.1 and abs(got["WELL-0100"] - 8.1) < 0.1


def test_report_facts_coverage(conn):
    n, f, c = conn.execute("SELECT COUNT(*), SUM(report_found), SUM(containment_hours IS NOT NULL) FROM report_facts").fetchone()
    assert n == f > 1000 and c > 0
    assert conn.execute("SELECT reporting_duty_listed FROM report_facts WHERE maintenance_id='MNT-0001-029'").fetchone()[0] == 1
    assert conn.execute("SELECT spill_class FROM report_facts WHERE maintenance_id='MNT-0151-025'").fetchone()[0] == "moderate"


def test_raw_never_overwritten(conn):
    r = conn.execute("SELECT spill_detected, raw_spill_detected, corrected FROM inspection_effective "
                     "WHERE inspection_id='INS-0001-0070'").fetchone()
    assert r["raw_spill_detected"] == 1 and r["spill_detected"] == 0 and r["corrected"] == 1


def test_doc_search(conn):
    from app.docsearch import DocIndex
    hits = DocIndex.from_db(conn).search("spill containment time limit hours", k=5, doc_type="sop")
    assert hits and any(h["chunk_id"].endswith("SR-02") for h in hits)
