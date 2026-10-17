"""SOP compliance logic. Thresholds come from the rules table; all date math happens here."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import config, metrics, rules


def rule_value(conn, key: str, well_type: str | None = None, scope_id: str = "default") -> float | None:
    """Single choke point for thresholds: manual/SOP rules with confirmed user well-type rules layered on top."""
    return rules.get_rule(conn, key, well_type, scope_id)


def rule_info(conn, key: str, well_type: str) -> dict:
    """Value plus where it came from, so answers can say 'your rule' versus 'the manual'."""
    r = conn.execute("SELECT * FROM rules_effective WHERE rule_key=? AND scope_type='well_type' AND scope_id=?",
                     (key, well_type)).fetchone()
    if r and str(r["source"]).startswith("user:"):
        uid = r["source"][5:]
        u = conn.execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
        m = conn.execute("SELECT value_num, source, section FROM rules WHERE rule_key=? AND scope_type='well_type' "
                         "AND scope_id=?", (key, well_type)).fetchone()
        return {"value": r["value_num"], "source": "user_rule", "fact_id": r["section"], "author_user_id": uid,
                "author_name": u[0] if u else uid, "text": r["description"],
                "manual_value": m["value_num"] if m else None,
                "manual_source": f"{m['source']}#{m['section']}" if m else None}
    return {"value": rule_value(conn, key, well_type), "source": "manual",
            "manual_source": f"{r['source']}#{r['section']}" if r else None}


def spill_class(conn, volume: float | None) -> str | None:
    if volume is None:
        return None
    rows = conn.execute("SELECT scope_id, value_num FROM rules WHERE rule_key='spill_class_min_bbl' "
                        "ORDER BY value_num DESC").fetchall()
    for r in rows:
        if volume >= r["value_num"]:
            return r["scope_id"]
    return None


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def _end_of_today() -> datetime:
    return datetime.combine(config.today(), datetime.max.time()).replace(microsecond=0)


def _rank(conn, cls: str | None) -> int:
    r = conn.execute("SELECT value_num FROM rules WHERE rule_key='spill_class_min_bbl' AND scope_id=?", (cls,)).fetchone()
    return r[0] if r else -1


def compliance_check(conn, well_id: str, start: str, end: str) -> dict:
    sc = metrics.resolve_scope(conn, well_id)
    if sc["kind"] != "well":
        return {"error": f"{well_id!r} is not a known well"}
    wid = sc["id"]
    well = conn.execute("SELECT * FROM wells WHERE asset_id=?", (wid,)).fetchone()
    wt = well["well_type"]
    now = _end_of_today()
    breaches, open_actions, met = [], [], []

    reportable_min = rule_value(conn, "spill_class_min_bbl", None, "reportable")
    major_min = rule_value(conn, "spill_class_min_bbl", None, "major")
    sr01_default = rule_value(conn, "sr01_report_hours", None, "default")
    sr01_major = rule_value(conn, "sr01_report_hours", None, "major")
    sr02 = rule_value(conn, "sr02_containment_hours")
    sr03_days = rule_value(conn, "sr03_verify_days")
    rc04_days = rule_value(conn, "rc04_nov_response_days")

    spills = conn.execute(
        """SELECT * FROM inspection_effective WHERE asset_id=? AND inspected_date BETWEEN ? AND ?
           AND spill_detected=1 ORDER BY inspected_at""", (wid, start, end)).fetchall()
    events = [dict(r) for r in conn.execute("SELECT * FROM regulatory_events WHERE asset_id=? ORDER BY date", (wid,))]

    spill_summaries = []
    for s in spills:
        iid, found = s["inspection_id"], _dt(s["inspected_at"])
        vol = s["spill_est_volume_bbl"]
        cls = spill_class(conn, vol)
        rep = conn.execute(
            """SELECT f.* FROM report_facts f JOIN maintenance m USING(maintenance_id)
               WHERE m.asset_id=? AND (m.triggered_by_inspection_id=? OR f.discovery_inspection_id=?)
               ORDER BY (f.containment_hours IS NULL), m.started_at LIMIT 1""", (wid, iid, iid)).fetchone()
        info = {"inspection_id": iid, "date": s["inspected_date"], "volume_bbl": vol, "class": cls,
                "volume_corrected": s["volume_correction_id"] is not None,
                "report_id": rep["maintenance_id"] if rep else None,
                "report_class": rep["spill_class"] if rep else None,
                "report_class_outdated": bool(rep and rep["spill_class"] and cls and rep["spill_class"] != cls)}
        spill_summaries.append(info)
        ev = [e for e in events if e["related_inspection_id"] == iid]

        if rep and rep["containment_hours"] is not None and sr02 is not None:
            item = {"rule": "SR-02", "spill": iid, "containment_hours": rep["containment_hours"], "limit_hours": sr02,
                    "evidence": [rep["maintenance_id"], iid]}
            (breaches if rep["containment_hours"] > sr02 else met).append(
                {**item, "detail": f"contained after about {rep['containment_hours']} h vs {sr02:g} h limit"})

        if vol is not None and reportable_min is not None and vol >= reportable_min:
            limit = sr01_major if (major_min is not None and vol >= major_min) else sr01_default
            filed = [e for e in ev if e["event_type"] == "spill_report_filed"]
            if filed:
                h = filed[0]["hours_to_report"]
                item = {"rule": "SR-01", "spill": iid, "hours_to_report": h, "limit_hours": limit,
                        "evidence": [filed[0]["event_id"], iid]}
                (breaches if h is not None and h > limit else met).append(
                    {**item, "detail": f"reported after {h} h vs {limit:g} h limit"})
            else:
                due = found + timedelta(hours=limit)
                item = {"rule": "SR-01", "spill": iid, "limit_hours": limit, "notice_due": due.isoformat(timespec="minutes"),
                        "evidence": [iid], "detail": f"reportable spill ({vol} bbl); no written notice recorded; due {due:%d %b %Y %H:%M}"}
                (breaches if now > due else open_actions).append(
                    {**item, "status": "possible breach: no notice recorded" if now > due else "notice still due"})
            if sr03_days is not None:
                due = found.date() + timedelta(days=int(sr03_days))
                ins_ev = [e for e in ev if e["event_type"] == "site_inspection"]
                closed = [e for e in ins_ev if e["status"] == "closed"]
                item = {"rule": "SR-03", "spill": iid, "verification_due": due.isoformat(),
                        "evidence": [e["event_id"] for e in ins_ev] or [iid]}
                if closed:
                    ok = date.fromisoformat(closed[0]["date"]) <= due
                    (met if ok else breaches).append({**item, "detail": f"verified {closed[0]['date']} (due {due})"})
                elif config.today() > due:
                    breaches.append({**item, "detail": f"cleanup verification was due {due}; still open"})
                else:
                    open_actions.append({**item, "detail": f"cleanup verification due by {due}"})

    # RC-04: notices of violation need a response within N days
    for e in events:
        if e["event_type"] != "notice_of_violation" or not (start <= e["date"] <= end or e["status"] == "open"):
            continue
        due = date.fromisoformat(e["date"]) + timedelta(days=int(rc04_days or 30))
        resp = [r for r in events if r["event_type"] == "nov_response_filed" and r["date"] >= e["date"]
                and (r["related_inspection_id"] in (None, e["related_inspection_id"]))]
        item = {"rule": "RC-04", "notice": e["event_id"], "response_due": due.isoformat(), "fine_usd": e["fine_usd"],
                "status": e["status"], "evidence": [e["event_id"]] + [r["event_id"] for r in resp]}
        if resp:
            late = date.fromisoformat(resp[0]["date"]) > due
            (breaches if late else met).append({**item, "detail": f"response filed {resp[0]['date']} (due {due})"})
        elif config.today() > due:
            breaches.append({**item, "detail": f"response was due {due}; none recorded"})
        else:
            open_actions.append({**item, "detail": f"response due by {due}"})

    # RC-02: one flowline integrity test per calendar year
    for yr in range(date.fromisoformat(start).year, date.fromisoformat(end).year + 1):
        t = conn.execute("""SELECT maintenance_id, started_date FROM maintenance WHERE asset_id=?
                            AND LOWER(issue) LIKE '%flowline%test%' AND started_date BETWEEN ? AND ?""",
                         (wid, f"{yr}-01-01", f"{yr}-12-31")).fetchall()
        item = {"rule": "RC-02", "year": yr}
        if t:
            met.append({**item, "detail": f"flowline test done {t[0]['started_date']}", "evidence": [t[0]["maintenance_id"]]})
        elif yr < config.today().year:
            breaches.append({**item, "detail": f"no flowline integrity test recorded in {yr}", "evidence": []})
        else:
            open_actions.append({**item, "detail": f"no flowline integrity test yet in {yr}", "evidence": []})

    # RC-01: inspect at least every N days
    rc01 = rule_value(conn, "rc01_inspection_interval_days")
    if rc01:
        prev, gaps = None, []
        for r in conn.execute("SELECT inspection_id, inspected_at FROM inspections WHERE asset_id=? "
                              "AND inspected_date BETWEEN ? AND ? ORDER BY inspected_at", (wid, start, end)):
            cur = _dt(r["inspected_at"])
            if prev and (cur - prev[1]).days > rc01:
                gaps.append({"from": prev[0], "to": r["inspection_id"], "days": (cur - prev[1]).days})
            prev = (r["inspection_id"], cur)
        for g in gaps:
            breaches.append({"rule": "RC-01", "detail": f"{g['days']} days between inspections (limit {rc01:g})",
                             "evidence": [g["from"], g["to"]]})

    # RC-03: down for more than N consecutive days must be reported to the supervisor
    rc03 = rule_value(conn, "rc03_report_downtime_days")
    if rc03:
        run: list[str] = []
        runs = []
        for r in conn.execute("SELECT inspection_id, producing FROM inspection_effective WHERE asset_id=? "
                              "AND inspected_date BETWEEN ? AND ? ORDER BY inspected_at", (wid, start, end)):
            if r["producing"] == 0:
                run.append(r["inspection_id"])
            else:
                runs.append(run) if run else None
                run = []
        if run:
            runs.append(run)
        for rn in runs:
            if len(rn) * config.DAYS_PER_INSPECTION > rc03:
                open_actions.append({"rule": "RC-03", "detail": f"down about {len(rn) * config.DAYS_PER_INSPECTION} days "
                                     f"(> {rc03:g}); supervisor report required, none can be verified in the data",
                                     "evidence": rn})

    for r in rca_due(conn, wid, end):
        open_actions.append({"rule": "RCA", **r})

    open_fines = sum(e["fine_usd"] or 0 for e in events if e["status"] == "open" and e["event_type"] == "notice_of_violation")
    return {"well_id": wid, "well_type": wt, "period": {"start": start, "end": end}, "spills_in_period": spill_summaries,
            "breaches": breaches, "open_actions": open_actions, "rules_met": met,
            "open_fines_usd": open_fines,
            "evidence_ids": sorted({x for b in breaches + open_actions + met for x in b.get("evidence", [])})}


def rca_due(conn, scope: str, as_of: str | None = None) -> list[dict]:
    """Root-cause-analysis triggers: N corrective repairs on the same equipment kind in the window."""
    sc = metrics.resolve_scope(conn, scope)
    end = as_of or config.today().isoformat()
    out = []
    for w in conn.execute(f"SELECT asset_id, well_type FROM wells WHERE asset_id IN ({metrics._in(sc['wells'])})", sc["wells"]):
        trig = rule_value(conn, "rca_trigger_corrective_count", w["well_type"])
        rinfo = rule_info(conn, "rca_trigger_corrective_count", w["well_type"])
        months = rule_value(conn, "rca_window_months", w["well_type"]) or 12
        if not trig:
            continue
        start = metrics.add_months(date.fromisoformat(end), -int(months)).isoformat()
        rows = conn.execute(
            """SELECT m.equipment_kind, GROUP_CONCAT(m.maintenance_id) ids, COUNT(*) n,
                      MAX(COALESCE(f.action_required, 0)) action_flag
               FROM maintenance m LEFT JOIN report_facts f USING(maintenance_id)
               WHERE m.asset_id=? AND m.maintenance_type='corrective' AND m.started_date > ? AND m.started_date <= ?
               GROUP BY m.equipment_kind HAVING COUNT(*) >= ?""", (w["asset_id"], start, end, trig)).fetchall()
        for r in rows:
            out.append({"well_id": w["asset_id"], "well_type": w["well_type"], "equipment_kind": r["equipment_kind"],
                        "corrective_repairs": r["n"], "trigger_count": int(trig), "window_months": int(months),
                        "report_marks_action_required": bool(r["action_flag"]),
                        "rule_source": rinfo["source"],
                        **({"user_rule": {k: rinfo[k] for k in ("fact_id", "author_name", "text", "manual_value")}}
                           if rinfo["source"] == "user_rule" else {}),
                        "detail": f"{r['n']} corrective repairs on {r['equipment_kind']} in {int(months)} months "
                                  f"(trigger {int(trig)}{', per ' + rinfo['author_name'] + chr(39) + 's rule, not the manual' if rinfo['source'] == 'user_rule' else ''}): root-cause analysis due",
                        "evidence": r["ids"].split(",")})
    return out
