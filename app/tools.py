"""Agent tools: typed functions returning structured JSON with the record IDs used as evidence.

The model picks tools and writes prose; every number and date comes from here.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date

from . import compliance, config, metrics
from .docsearch import DocIndex

SHUTDOWN_RE = re.compile(r"shut[\s-]?in|shut\s?down|workover|offline|out of service|restart|rig", re.I)
_index_cache: dict = {}


def _scope_ids(conn, scope):
    sc = metrics.resolve_scope(conn, scope)
    if sc["kind"] == "unknown":
        raise ValueError(f"unknown scope {scope!r}; use a well id, an area id/name, a well type or 'all'")
    return sc


def _active_notes(conn, wid: str) -> list[dict]:
    t = config.today().isoformat()
    rows = conn.execute(
        """SELECT note_id, created_at, author_user_id, note_type, inspection_id, text, valid_from, valid_to
           FROM notes WHERE asset_id=? AND note_type != 'correction'
             AND (valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to >= ?)
           ORDER BY valid_from""", (wid, t, t)).fetchall()
    return [{**dict(r), "looks_like_shutdown": bool(SHUTDOWN_RE.search(r["text"] or ""))} for r in rows]


def _open_repairs(conn, wid: str) -> list[dict]:
    t = config.today().isoformat()
    return [dict(r) for r in conn.execute(
        """SELECT maintenance_id, started_at, completed_at, maintenance_type, equipment_kind, issue
           FROM maintenance WHERE asset_id=? AND started_date <= ? AND SUBSTR(completed_at,1,10) > ?
           ORDER BY started_at""", (wid, t, t))]


def _user_name(conn, uid):
    r = conn.execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r else None


def _latest(conn, wid: str):
    return conn.execute("SELECT * FROM inspection_effective WHERE asset_id=? ORDER BY inspected_at DESC LIMIT 1",
                        (wid,)).fetchone()


def _status_block(conn, wid: str) -> dict:
    w = conn.execute("SELECT * FROM wells WHERE asset_id=?", (wid,)).fetchone()
    i = _latest(conn, wid)
    out = {"well_id": wid, "well_name": w["name"], "well_type": w["well_type"], "area_id": w["area_id"],
           "recorded_status": w["recorded_status"], "lat": w["lat"], "lon": w["lon"]}
    if not i:
        return {**out, "latest_inspection": None, "producing": None, "note": "no inspections on record"}
    producing = i["producing"]
    real = None if producing is None else ("producing" if producing else "not producing")
    stale = None
    if producing is not None and w["recorded_status"]:
        rs = w["recorded_status"].lower()
        stale = (producing == 0 and rs == "active") or (producing == 1 and rs in ("down", "inactive", "shut_in"))
    notes = conn.execute("SELECT note_id, author_user_id, text FROM notes WHERE inspection_id=? "
                         "AND note_type != 'correction'", (i["inspection_id"],)).fetchall()
    out.update({
        "producing": None if producing is None else bool(producing), "real_status": real,
        "recorded_status_out_of_date": stale,
        "latest_inspection": {
            "inspection_id": i["inspection_id"], "inspected_at": i["inspected_at"], "method": i["method"],
            "inspector_user_id": i["inspector_user_id"], "inspector_name": _user_name(conn, i["inspector_user_id"]),
            "wellhead_temp_c": i["wellhead_temp_c"], "ambient_temp_c": i["ambient_temp_c"],
            "delta_c": i["delta_c"], "producing_threshold_delta_c": i["producing_min_delta_c"],
            "spill_detected": bool(i["spill_detected"]), "inspector_notes": i["notes"],
            "corrected": bool(i["corrected"]),
            "raw_wellhead_temp_c": i["raw_wellhead_temp_c"], "raw_ambient_temp_c": i["raw_ambient_temp_c"],
            "correction_ids": [c for c in (i["wellhead_correction_id"], i["ambient_correction_id"],
                                           i["spill_correction_id"], i["volume_correction_id"]) if c],
            "notes_on_this_inspection": [dict(n) for n in notes]},
        "days_since_inspection": (config.today() - date.fromisoformat(i["inspected_date"])).days})
    return out


def well_status(conn, well_id: str) -> dict:
    sc = _scope_ids(conn, well_id)
    if sc["kind"] != "well":
        raise ValueError(f"{well_id!r} is not a single well")
    wid = sc["id"]
    st = _status_block(conn, wid)
    notes, repairs = _active_notes(conn, wid), _open_repairs(conn, wid)
    st.update({"active_notes": notes, "planned_shutdowns": [n for n in notes if n["looks_like_shutdown"]],
               "open_repairs": repairs})
    ev = [st["latest_inspection"]["inspection_id"]] if st.get("latest_inspection") else []
    ev += [n["note_id"] for n in notes] + [r["maintenance_id"] for r in repairs]
    st["evidence_ids"] = ev + st.get("latest_inspection", {}).get("correction_ids", [])
    return st


def downtime(conn, scope: str, period: str | None = None, start: str | None = None, end: str | None = None,
             top_n: int | None = None) -> dict:
    start, end = metrics.resolve_range(period, start, end)
    r = metrics.downtime(conn, scope, start, end)
    if r["scope_kind"] == "unknown":
        raise ValueError(f"unknown scope {scope!r}")
    area_tot: dict = {}
    for w in r["wells"]:
        area_tot[w["area_id"]] = area_tot.get(w["area_id"], 0) + w["downtime_days"]
    r["by_area_days"] = area_tot
    r["well_count"] = len(r["wells"])
    if top_n:
        r["wells"] = r["wells"][:top_n]
    r["evidence_ids"] = sorted({e for w in r["wells"] for e in w["evidence_ids"]})
    return r


def spills(conn, scope: str, period: str | None = None, start: str | None = None, end: str | None = None,
           include_corrected: bool = False, group_by: str = "none") -> dict:
    start, end = metrics.resolve_range(period, start, end)
    r = metrics.spills(conn, scope, start, end, include_corrected)
    if r["scope_kind"] == "unknown":
        raise ValueError(f"unknown scope {scope!r}")
    wells = {w["asset_id"]: w for w in conn.execute("SELECT asset_id, lat, lon, area_id FROM wells")}
    for s in r["spills"]:
        w = wells.get(s["asset_id"])
        s["lat"], s["lon"] = (w["lat"], w["lon"]) if w else (None, None)
    r["evidence_ids"] = [s["inspection_id"] for s in r["spills"]] + [c for e in r["excluded_by_correction"]
                                                                      for c in (e["inspection_id"], e["correction_id"])]
    if group_by in ("area", "month", "area_month"):
        area_names = {a["area_id"]: a["name"] for a in conn.execute("SELECT area_id, name FROM areas")}
        areas = sorted({w["area_id"] for w in wells.values() if r["scope_kind"] in ("fleet", "well_type")
                        or w["area_id"] == r["scope"] or w["asset_id"] == r["scope"]} & set(area_names))
        months = _months(start, end)
        grid = {a: {m: 0 for m in months} for a in areas}
        for s in r["spills"]:
            grid.setdefault(s["area_id"], {m: 0 for m in months})[s["inspected_date"][:7]] += 1
        r["heatmap"] = {"rows": [area_names.get(a, a) for a in grid], "row_ids": list(grid), "columns": months,
                        "values": [[grid[a][m] for m in months] for a in grid],
                        "row_totals": [sum(grid[a].values()) for a in grid]}
    return r


def _months(start: str, end: str) -> list[str]:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    out, y, m = [], s.year, s.month
    while (y, m) <= (e.year, e.month):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def list_wells_not_producing(conn, scope: str = "all") -> dict:
    sc = _scope_ids(conn, scope)
    out = []
    for wid in sc["wells"]:
        st = _status_block(conn, wid)
        if st.get("producing") is not False:
            continue
        notes, repairs = _active_notes(conn, wid), _open_repairs(conn, wid)
        reasons = [{"type": "open_repair", "id": r["maintenance_id"], "text": r["issue"], "started_at": r["started_at"],
                    "expected_completion": r["completed_at"]} for r in repairs]
        reasons += [{"type": "note", "id": n["note_id"], "text": n["text"], "author_user_id": n["author_user_id"]}
                    for n in notes if n["looks_like_shutdown"]]
        item = {**st, "reasons": reasons, "other_active_notes": [n for n in notes if not n["looks_like_shutdown"]]}
        if not reasons:
            item["reason_summary"] = "no recorded reason"
            item["suggested_check"] = ("Send a crew or request a fresh inspection to confirm the stop, and check the "
                                       "pump jack/prime mover and flowline; no open repair or shut-in note explains it.")
        out.append(item)
    ev = [w["latest_inspection"]["inspection_id"] for w in out] + [r["id"] for w in out for r in w["reasons"]]
    return {"scope": sc["id"], "scope_kind": sc["kind"], "as_of": config.today().isoformat(),
            "count": len(out), "wells": out, "evidence_ids": ev}


def compliance_check(conn, well_id: str, period: str | None = None, start: str | None = None, end: str | None = None) -> dict:
    start, end = metrics.resolve_range(period, start, end)
    return compliance.compliance_check(conn, well_id, start, end)


def root_cause_triggers(conn, scope: str = "all") -> dict:
    sc = _scope_ids(conn, scope)
    rows = compliance.rca_due(conn, scope if sc["kind"] != "fleet" else "all")
    return {"scope": sc["id"], "as_of": config.today().isoformat(), "count": len(rows), "triggers": rows,
            "wells": sorted({r["well_id"] for r in rows}),
            "evidence_ids": sorted({e for r in rows for e in r["evidence"]})}


def repair_costs(conn, scope: str, period: str | None = None, start: str | None = None, end: str | None = None,
                 maintenance_type: str | None = None) -> dict:
    start, end = metrics.resolve_range(period, start, end)
    sc = _scope_ids(conn, scope)
    ids = sc["wells"]
    q = f"""SELECT asset_id, COUNT(*) n, SUM(cost_labor) labor, SUM(cost_parts) parts, SUM(cost_total) total,
                   GROUP_CONCAT(maintenance_id) ids
            FROM maintenance WHERE asset_id IN ({metrics._in(ids)}) AND started_date BETWEEN ? AND ?"""
    args = [*ids, start, end]
    if maintenance_type:
        q += " AND maintenance_type=?"
        args.append(maintenance_type)
    rows = conn.execute(q + " GROUP BY asset_id ORDER BY total DESC", args).fetchall()
    wells = [{"asset_id": r["asset_id"], "repairs": r["n"], "labor_usd": r["labor"], "parts_usd": r["parts"],
              "total_usd": r["total"], "maintenance_ids": r["ids"].split(",")} for r in rows]
    return {"scope": sc["id"], "scope_kind": sc["kind"], "start": start, "end": end, "maintenance_type": maintenance_type,
            "total_usd": sum(w["total_usd"] or 0 for w in wells), "wells": wells,
            "evidence_ids": [m for w in wells for m in w["maintenance_ids"]]}


def inspection_history(conn, well_id: str, period: str | None = None, start: str | None = None,
                       end: str | None = None, last_n: int | None = None) -> dict:
    sc = _scope_ids(conn, well_id)
    wid = sc["id"]
    if last_n:
        rows = conn.execute("SELECT * FROM (SELECT * FROM inspection_effective WHERE asset_id=? "
                            "ORDER BY inspected_at DESC LIMIT ?) ORDER BY inspected_at", (wid, last_n)).fetchall()
    else:
        start, end = metrics.resolve_range(period, start, end)
        rows = conn.execute("SELECT * FROM inspection_effective WHERE asset_id=? AND inspected_date BETWEEN ? AND ? "
                            "ORDER BY inspected_at", (wid, start, end)).fetchall()
    pts = [{"inspection_id": r["inspection_id"], "date": r["inspected_date"], "wellhead_temp_c": r["wellhead_temp_c"],
            "ambient_temp_c": r["ambient_temp_c"], "delta_c": r["delta_c"], "producing": None if r["producing"] is None else bool(r["producing"]),
            "threshold_delta_c": r["producing_min_delta_c"], "corrected": bool(r["corrected"])} for r in rows]
    return {"well_id": wid, "points": pts, "not_producing_dates": [p["date"] for p in pts if p["producing"] is False],
            "evidence_ids": [p["inspection_id"] for p in pts]}


def _index(conn):
    key = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    k = (path, key)
    if k not in _index_cache:
        _index_cache.clear()
        _index_cache[k] = DocIndex.from_db(conn)
    return _index_cache[k]


def search_docs(conn, query: str, k: int = 5, doc_type: str | None = None, well_id: str | None = None) -> dict:
    hits = _index(conn).search(query, k=k, doc_type=doc_type, asset_id=well_id)
    return {"query": query, "results": hits, "evidence_ids": [h["chunk_id"] for h in hits]}


def get_rules(conn, well_type: str | None = None) -> dict:
    rows = conn.execute(
        """SELECT rule_key, scope_type, scope_id, value_num, unit, source, section, description FROM rules
           WHERE scope_type='global' OR ?1 IS NULL OR scope_id=?1 ORDER BY rule_key, scope_id""", (well_type,)).fetchall()
    return {"well_type": well_type, "rules": [dict(r) for r in rows],
            "evidence_ids": sorted({f"{r['source']}#{r['section']}" for r in rows if r["source"]})}


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def sql_query(conn, query: str, limit: int = 200) -> dict:
    q = query.strip().rstrip(";")
    if ";" in q or not re.match(r"(?is)^(select|with)\b", q):
        raise ValueError("only a single SELECT statement is allowed")
    conn.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY)
    try:
        cur = conn.execute(q)
        rows = cur.fetchmany(limit + 1)
        cols = [d[0] for d in cur.description]
    finally:
        conn.set_authorizer(None)
    return {"columns": cols, "rows": [list(r) for r in rows[:limit]], "truncated": len(rows) > limit,
            "note": "Tables: wells, inspections, inspection_effective (use this for producing/spill), maintenance, "
                    "report_facts, regulatory_events, notes, rules, areas, users, corrections."}


# ---- registry ------------------------------------------------------------------------------

_PERIOD = {"period": {"type": "string", "description": "Relative period such as 'last_4_months', 'last_90_days', "
                      "'last_13_weeks', '2025', 'year_to_date'. Preferred over start/end; never compute dates yourself."},
           "start": {"type": "string", "description": "YYYY-MM-DD (only if period is not enough)"},
           "end": {"type": "string", "description": "YYYY-MM-DD"}}
_SCOPE = {"type": "string", "description": "A well id (WELL-0001), an area id or name, a well type (T200), or 'all'"}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOLS: dict[str, dict] = {
    "well_status": {"fn": well_status, "description": "Current status of one well from its latest inspection (producing or "
                    "not, temperature delta vs threshold), recorded status vs real status, active notes, planned "
                    "shutdowns and open repairs.", "parameters": _obj({"well_id": {"type": "string"}}, ["well_id"])},
    "downtime": {"fn": downtime, "description": "Downtime days per well for a well, area, well type or all, from inspections "
                 "(each non-producing inspection = 7 days). Use top_n for rankings.",
                 "parameters": _obj({"scope": _SCOPE, **_PERIOD, "top_n": {"type": "integer"}}, ["scope"])},
    "spills": {"fn": spills, "description": "Spill counts and volumes (corrections applied unless include_corrected=true). "
               "group_by 'area_month' returns a heatmap matrix; items include lat/lon for maps.",
               "parameters": _obj({"scope": _SCOPE, **_PERIOD, "include_corrected": {"type": "boolean"},
                                   "group_by": {"type": "string", "enum": ["none", "area", "month", "area_month"]}}, ["scope"])},
    "list_wells_not_producing": {"fn": list_wells_not_producing, "description": "Wells currently not producing with reasons from "
                                 "open repairs and notes; says 'no recorded reason' when none exists.",
                                 "parameters": _obj({"scope": _SCOPE}, [])},
    "compliance_check": {"fn": compliance_check, "description": "Check one well's spills and regulator events against the SOP "
                         "rules (SR-01..03, RC-01..04, root-cause triggers) for a period. Returns breaches and open actions.",
                         "parameters": _obj({"well_id": {"type": "string"}, **_PERIOD}, ["well_id"])},
    "root_cause_triggers": {"fn": root_cause_triggers, "description": "Wells whose corrective repair count on one equipment kind "
                            "reached the root-cause-analysis trigger (per-well-type rule).",
                            "parameters": _obj({"scope": _SCOPE}, [])},
    "repair_costs": {"fn": repair_costs, "description": "Repair cost totals per well for a scope and period.",
                     "parameters": _obj({"scope": _SCOPE, **_PERIOD, "maintenance_type": {"type": "string", "enum": ["corrective", "scheduled"]}}, ["scope"])},
    "inspection_history": {"fn": inspection_history, "description": "Time series of wellhead/ambient temperature and producing "
                           "flag for one well (for line charts). Use last_n for 'last 13 weeks' (weekly inspections).",
                           "parameters": _obj({"well_id": {"type": "string"}, **_PERIOD, "last_n": {"type": "integer"}}, ["well_id"])},
    "search_docs": {"fn": search_docs, "description": "BM25 search over manuals, SOPs and repair reports; returns chunk ids to cite.",
                    "parameters": _obj({"query": {"type": "string"}, "k": {"type": "integer"},
                                        "doc_type": {"type": "string", "enum": ["manual", "sop", "report"]},
                                        "well_id": {"type": "string"}}, ["query"])},
    "get_rules": {"fn": get_rules, "description": "Thresholds and limits from the rules table (manual/SOP), optionally for a well type.",
                  "parameters": _obj({"well_type": {"type": "string"}}, [])},
    "sql_query": {"fn": sql_query, "description": "Read-only SELECT escape hatch for ad hoc analysis. Prefer the other tools.",
                  "parameters": _obj({"query": {"type": "string"}}, ["query"])},
}


def tool_specs() -> list[dict]:
    return [{"name": n, "description": t["description"], "parameters": t["parameters"]} for n, t in TOOLS.items()]


def call_tool(conn, name: str, args: dict | None) -> dict:
    t = TOOLS.get(name)
    if not t:
        return {"error": f"unknown tool {name!r}", "available": list(TOOLS)}
    args = dict(args or {})
    missing = [p for p in t["parameters"]["required"] if p not in args]
    if missing:
        return {"error": f"missing required arguments: {missing}"}
    allowed = t["parameters"]["properties"]
    unknown = [a for a in args if a not in allowed]
    if unknown:
        return {"error": f"unknown arguments {unknown}; allowed: {list(allowed)}"}
    try:
        return t["fn"](conn, **args)
    except (ValueError, sqlite3.Error) as e:
        return {"error": str(e)}
