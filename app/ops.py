"""Operations-procedure tools (OPS-1..OPS-6, MOC memos, supplier recall, regulator letter).

All parameters (deadlines, lots, expiry dates, windows) are parsed from the SOP/memo text in doc_chunks at query time,
so a changed or new dataset needs no code change. Counting and date math live here, never in the model.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime, timedelta, timezone

from . import config, memory, metrics

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_RE = r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})"
GAS_RE = re.compile(r"\bgas\b|rotten[- ]egg|h2s|hns monitor", re.I)
BYPASS_RE = re.compile(r"bypass", re.I)
BYPASS_OUT_RE = re.compile(r"\bout\b|removed|pulled", re.I)
SHUT_RE = re.compile(r"shut[\s-]?(?:the well\s+)?in\b|shut[\s-]in", re.I)
RESTART_RE = re.compile(r"restarted|put back on production|back online", re.I)
NEUTRAL_RE = re.compile(r"normally|nothing to report|dry and clean|level normal|running steady|readings normal", re.I)


def _d(s: str) -> date:
    m = re.search(_DATE_RE, s)
    if not m:
        raise ValueError(f"cannot parse date from {s!r}")
    return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _hours(a: str, b: str) -> float:
    return round((_ts(b) - _ts(a)).total_seconds() / 3600, 1)


def _sop_docs(conn) -> dict[str, dict]:
    docs: dict[str, dict] = {}
    for r in conn.execute("SELECT doc_id, chunk_id, text FROM doc_chunks WHERE doc_type='sop' ORDER BY rowid"):
        d = docs.setdefault(r["doc_id"], {"text": "", "chunks": []})
        d["text"] += r["text"] + "\n"
        d["chunks"].append(r["chunk_id"])
    return docs


def _area_name(conn, area_id):
    r = conn.execute("SELECT name FROM areas WHERE area_id=?", (area_id,)).fetchone()
    return r[0] if r else area_id


def _uname(conn, uid):
    r = conn.execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r else uid


# ---- OPS-1 gas odor ---------------------------------------------------------------------------
def gas_odor_responses(conn, since: str, until: str | None = None) -> dict:
    since_d = date.fromisoformat(since[:10])
    until_d = date.fromisoformat(until[:10]) if until else config.today()
    sop = _sop_docs(conn).get("SOP-OPS") or next((d for d in _sop_docs(conn).values() if "OPS-1" in d["text"]), None)
    if not sop:
        raise ValueError("no operations SOP with OPS-1 found in the documents")
    t = sop["text"]
    old = re.search(r"\*\*(\d+) hours\*\* for reports noted before " + _DATE_RE, t)
    new = re.search(r"\*\*(\d+) hours\*\* for reports noted from " + _DATE_RE, t)
    if not (old and new):
        raise ValueError("could not parse the OPS-1 deadlines from the SOP text")
    old_h, new_h = int(old.group(1)), int(new.group(1))
    switch = _d(new.group(0))
    cutoff = until_d + timedelta(days=1)
    rows = conn.execute(
        """SELECT n.note_id, n.asset_id, n.created_at, n.text, w.area_id FROM notes n JOIN wells w ON w.asset_id=n.asset_id
           WHERE n.note_type='operator_note' AND SUBSTR(n.created_at,1,10) >= ? AND SUBSTR(n.created_at,1,10) <= ?
           ORDER BY n.created_at""", (since_d.isoformat(), until_d.isoformat())).fetchall()
    now = datetime.combine(cutoff, datetime.min.time(), tzinfo=timezone.utc)
    out, met = [], 0
    for n in rows:
        if not GAS_RE.search(n["text"]):
            continue
        deadline = new_h if _ts(n["created_at"]).date() >= switch else old_h
        cand = []
        for m in conn.execute("SELECT maintenance_id, started_at FROM maintenance WHERE asset_id=? AND started_at > ? "
                              "ORDER BY started_at LIMIT 1", (n["asset_id"], n["created_at"])):
            cand.append((m["started_at"], m["maintenance_id"], "maintenance job started"))
        for i in conn.execute("SELECT inspection_id, inspected_at FROM inspections WHERE asset_id=? AND method='manual' "
                              "AND inspected_at > ? ORDER BY inspected_at LIMIT 1", (n["asset_id"], n["created_at"])):
            cand.append((i["inspected_at"], i["inspection_id"], "manual inspection"))
        cand.sort()
        row = {"well_id": n["asset_id"], "area": _area_name(conn, n["area_id"]), "note_id": n["note_id"],
               "reported_at": n["created_at"], "note_text": n["text"], "deadline_hours": deadline}
        if cand:
            at, rid, kind = cand[0]
            h = _hours(n["created_at"], at)
            ok = h <= deadline
            row.update(response_id=rid, response_kind=kind, response_at=at, hours_to_response=h,
                       result="Met" if ok else "Missed")
            met += ok
        else:
            pending = _ts(n["created_at"]) + timedelta(hours=deadline) > now
            row.update(response_id=None, response_at=None, hours_to_response=None, result="Open" if pending else "Missed")
        out.append(row)
    return {"rule": f"OPS-1: response within {old_h}h for reports before {switch.isoformat()}, {new_h}h from then; "
                    "a response is a maintenance job start or a manual (not drone) inspection after the note",
            "since": since_d.isoformat(), "until": until_d.isoformat(), "reports": len(out), "met": met,
            "missed": sum(1 for r in out if r["result"] == "Missed"), "rows": out,
            "evidence_ids": [r["note_id"] for r in out] + [r["response_id"] for r in out if r["response_id"]] + sop["chunks"][:1]}


# ---- OPS-2 bypasses and MOC memos -------------------------------------------------------------
def _mocs(conn) -> dict[str, dict]:
    memos: dict[str, dict] = {}
    for doc_id, d in _sop_docs(conn).items():
        t = d["text"]
        m = re.search(r"Management of Change\s*\S*\s*(MOC-[\w-]+)", t) or re.search(r"\b(MOC-\d{4}-\d+)\b", t)
        if not m or "Management of Change" not in t:
            continue
        eff = re.search(r"Effective from " + _DATE_RE, t)
        exp = re.search(r"expires on " + _DATE_RE, t)
        ext = re.search(r"extends\s+(MOC-\d{4}-\d+)", t)
        memos[m.group(1)] = {"id": m.group(1), "wells": sorted(set(re.findall(r"WELL-\d+", t))),
                             "effective": _d(eff.group(0)).isoformat() if eff else None,
                             "expires": _d(exp.group(0)).isoformat() if exp else None,
                             "extends": ext.group(1) if ext else None, "chunk": d["chunks"][0]}
    return memos


def _effective_expiry(memos: dict, mid: str) -> tuple[str | None, list[str]]:
    exp, by = memos[mid]["expires"], []
    for o in memos.values():
        if o["extends"] == mid and o["expires"]:
            by.append(o["id"])
            exp = max(exp or "", o["expires"])
    return exp, by


def temporary_bypasses(conn) -> dict:
    memos = _mocs(conn)
    today = config.today()
    eps: dict[str, list[dict]] = {}
    for n in conn.execute("SELECT note_id, asset_id, created_at, text FROM notes WHERE note_type='operator_note' "
                          "ORDER BY created_at"):
        if not BYPASS_RE.search(n["text"]):
            continue
        out = bool(BYPASS_OUT_RE.search(n["text"]))
        lst = eps.setdefault(n["asset_id"], [])
        if not out:
            if not lst or lst[-1].get("out_at"):
                lst.append({"in_at": n["created_at"], "in_note": n["note_id"]})
        elif lst and not lst[-1].get("out_at"):
            lst[-1].update(out_at=n["created_at"], out_note=n["note_id"])
    rows = []
    for wid, lst in eps.items():
        w = conn.execute("SELECT area_id FROM wells WHERE asset_id=?", (wid,)).fetchone()
        for e in lst:
            cover = [m for m in memos.values() if wid in m["wells"]]
            base = [m for m in cover if not m["extends"]] or cover
            memo = base[0] if base else None
            exp, by = (_effective_expiry(memos, memo["id"]) if memo else (None, []))
            end = _ts(e["out_at"]).date() if e.get("out_at") else today
            if not memo:
                status = "Not approved"
            elif exp and end > date.fromisoformat(exp):
                status = "Expired"
            else:
                status = "Allowed"
            rows.append({"well_id": wid, "area": _area_name(conn, w["area_id"]) if w else None,
                         "bypass_in": e["in_at"][:10], "bypass_out": e["out_at"][:10] if e.get("out_at") else "still in",
                         "in_place_now": not e.get("out_at"),
                         "approval": (memo["id"] + (f" (extended by {', '.join(by)})" if by else "")) if memo else "none",
                         "approval_valid_until": exp, "status": status,
                         "evidence_ids": [e["in_note"]] + ([e["out_note"]] if e.get("out_note") else [])
                         + ([memo["chunk"]] if memo else [])})
    rows.sort(key=lambda r: (not r["in_place_now"], r["well_id"], r["bypass_in"]))
    return {"today": today.isoformat(),
            "rule": "OPS-2: a bypass is allowed only on a well named in an unexpired MOC memo; an extension memo replaces "
                    "the expiry; expiry is the end of that day; a bypass is in place from the put-in note until the take-out note",
            "memos": [{**m, "effective_expiry": _effective_expiry(memos, m["id"])[0]} for m in memos.values()],
            "in_place_now": [r for r in rows if r["in_place_now"]],
            "violations": [r for r in rows if r["status"] != "Allowed"], "all_episodes": rows,
            "evidence_ids": sorted({e for r in rows for e in r["evidence_ids"]})}


# ---- OPS-3 status precedence ------------------------------------------------------------------
def _latest_notes(conn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for n in conn.execute("SELECT note_id, asset_id, created_at, text FROM notes WHERE note_type='operator_note' "
                          "ORDER BY created_at"):
        s, r = SHUT_RE.search(n["text"]), RESTART_RE.search(n["text"])
        if not (s or r):
            continue
        kind = "shut_in" if (s and (not r or s.start() <= r.start())) else "restart"
        out[n["asset_id"]] = {"note_id": n["note_id"], "created_at": n["created_at"], "text": n["text"], "kind": kind}
    return out


def _latest_inspections(conn) -> dict[str, dict]:
    rows = conn.execute("""SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY asset_id ORDER BY inspected_at DESC) rn
                           FROM inspection_effective) WHERE rn=1""").fetchall()
    return {r["asset_id"]: dict(r) for r in rows}


def _resolve(ins: dict | None, note: dict | None) -> dict:
    """OPS-3: the more recent of the latest inspection reading and the latest shut-in / restart note wins."""
    insp_p = None if not ins or ins["producing"] is None else bool(ins["producing"])
    if note and (not ins or note["created_at"] > ins["inspected_at"]):
        if note["kind"] == "shut_in":
            return {"producing": False, "label": "Not producing oil", "basis": "newer operator shut-in note",
                    "newer_source": "operator_note"}
        return {"producing": True, "label": "Producing oil (unconfirmed)", "basis": "newer operator restart note, "
                "not yet confirmed by an inspection", "newer_source": "operator_note"}
    if insp_p is None:
        return {"producing": None, "label": "Unknown", "basis": "no inspection", "newer_source": None}
    return {"producing": insp_p, "label": "Producing oil" if insp_p else "Not producing oil",
            "basis": "latest inspection reading (newer than any shut-in/restart note)", "newer_source": "inspection"}


def effective_status(conn, wid: str) -> dict:
    ins = _latest_inspections(conn).get(wid)
    return _resolve(ins, _latest_notes(conn).get(wid))


def status_conflicts(conn, scope: str = "all") -> dict:
    ids = set(metrics.resolve_scope(conn, scope)["wells"])
    ins_all, notes = _latest_inspections(conn), _latest_notes(conn)
    rows = []
    ignored = 0
    for wid, note in notes.items():
        ins = ins_all.get(wid)
        if wid not in ids or not ins or ins["producing"] is None:
            continue
        note_p = note["kind"] == "restart"
        if note_p == bool(ins["producing"]):
            continue
        res = _resolve(ins, note)
        if res["newer_source"] != "operator_note":
            ignored += 1
        w = conn.execute("SELECT area_id, recorded_status FROM wells WHERE asset_id=?", (wid,)).fetchone()
        rows.append({"well_id": wid, "area": _area_name(conn, w["area_id"]), "recorded_status": w["recorded_status"],
                     "latest_inspection": {"id": ins["inspection_id"], "at": ins["inspected_at"], "method": ins["method"],
                                           "shows": "producing" if ins["producing"] else "not producing",
                                           "delta_c": ins["delta_c"], "threshold_c": ins["producing_min_delta_c"],
                                           "corrected": bool(ins["corrected"])},
                     "latest_operator_note": {"id": note["note_id"], "at": note["created_at"], "text": note["text"],
                                              "says": "shut in" if note["kind"] == "shut_in" else "restarted"},
                     "newer_source": res["newer_source"], "status_now": res["label"], "why": res["basis"],
                     "evidence_ids": [ins["inspection_id"], note["note_id"]]})
    rows.sort(key=lambda r: r["well_id"])
    return {"rule": "OPS-3: current status is the more recent of the latest inspection reading (after data corrections) and "
                    "the latest operator shut-in/restart note; a restart note alone is 'producing (unconfirmed)'",
            "count": len(rows), "note_newer_than_inspection": len(rows) - ignored, "inspection_newer_than_note": ignored, "rows": rows, "evidence_ids": [e for r in rows for e in r["evidence_ids"]]}


# ---- regulator letter ---------------------------------------------------------------------------
def regulator_letter_spills(conn) -> dict:
    docs = _sop_docs(conn)
    letter = next((d for d in docs.values() if "Commission" in d["text"] and "soil sample" in d["text"].lower()
                   and "reportable" in d["text"].lower()), None)
    if not letter:
        raise ValueError("no regulator letter about soil sampling found in the documents")
    t = letter["text"]
    since = _d(re.search(r"since\s+" + _DATE_RE, t).group(0))
    due = _d(re.search(r"by " + _DATE_RE, t).group(0))
    areas = [r for r in conn.execute("SELECT area_id, name FROM areas") if r["name"] and r["name"] in t]
    sentence = re.search(r'sentence\s+"([^"]+)"', t)
    sentence = sentence.group(1) if sentence else "Soil samples sent to the lab."
    thr = conn.execute("SELECT value_num FROM rules WHERE rule_key LIKE '%reportable%' AND value_num IS NOT NULL "
                       "ORDER BY value_num LIMIT 1").fetchone()
    thr = thr[0] if thr else 5.0
    rows, ev = [], list(letter["chunks"][:1])
    for a in areas:
        for s in conn.execute(
                """SELECT i.inspection_id, i.asset_id, i.inspected_at, i.spill_est_volume_bbl AS vol, i.volume_correction_id,
                          i.spill_correction_id FROM inspection_effective i JOIN wells w ON w.asset_id=i.asset_id
                   WHERE w.area_id=? AND i.spill_detected=1 AND i.inspected_date >= ? AND i.spill_est_volume_bbl >= ?
                   ORDER BY i.asset_id, i.inspected_at""", (a["area_id"], since.isoformat(), thr)):
            job = conn.execute(
                """SELECT m.maintenance_id, m.started_at, m.completed_at, e.soil_sample_sent, f.report_found
                   FROM maintenance m LEFT JOIN report_facts f ON f.maintenance_id=m.maintenance_id
                   LEFT JOIN report_extra e ON e.maintenance_id=m.maintenance_id
                   WHERE m.asset_id=? AND (f.discovery_inspection_id=? OR m.triggered_by_inspection_id=?)
                   ORDER BY m.started_at LIMIT 1""", (s["asset_id"], s["inspection_id"], s["inspection_id"])).fetchone()
            if not job:
                ok, why, jid = False, "no clean-up job or report on record for this spill", None
            elif not job["report_found"]:
                ok, why, jid = False, "clean-up job has no report", job["maintenance_id"]
            elif job["soil_sample_sent"]:
                ok, why, jid = True, f'clean-up report says "{sentence}"', job["maintenance_id"]
            else:
                ok, why, jid = False, f'clean-up report does not contain "{sentence}"', job["maintenance_id"]
            corr = s["volume_correction_id"] or s["spill_correction_id"]
            rows.append({"well_id": s["asset_id"], "area": a["name"], "inspection_id": s["inspection_id"],
                         "date": s["inspected_at"][:10], "volume_bbl": s["vol"], "cleanup_job": jid, "compliant": ok,
                         "reason": why, "volume_or_detection_corrected": corr})
            ev += [s["inspection_id"]] + ([jid] if jid else []) + ([corr] if corr else [])
    return {"letter_summary": {"areas": [a["name"] for a in areas], "spills_since": since.isoformat(),
                               "reportable_at_or_above_bbl": thr, "results_due": due.isoformat(),
                               "compliance_test": f'clean-up report contains "{sentence}"',
                               "excluded": "spills later corrected to not-spill/not-crude, and spills below the threshold "
                                           "after corrections"},
            "today": config.today().isoformat(), "covered_spills": len(rows),
            "not_compliant": sum(1 for r in rows if not r["compliant"]), "rows": rows, "evidence_ids": ev}


# ---- supplier recall ---------------------------------------------------------------------------
def _user_team(conn, user_id):
    r = conn.execute("SELECT team_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    return r[0] if r else None


def effective_team(conn, wid: str) -> str | None:
    t = config.today().isoformat()
    h = conn.execute("SELECT team_id FROM well_team_history WHERE asset_id=? AND from_date<=? AND (to_date IS NULL OR "
                     "to_date>=?) ORDER BY from_date DESC LIMIT 1", (wid, t, t)).fetchone()
    if h:
        return h[0]
    r = conn.execute("SELECT w.team_id, a.team_id AS at FROM wells w LEFT JOIN areas a ON a.area_id=w.area_id "
                     "WHERE w.asset_id=?", (wid,)).fetchone()
    return (r["team_id"] or r["at"]) if r else None


def _team_ids(conn, team: str) -> str | None:
    r = conn.execute("SELECT team_id FROM teams WHERE LOWER(team_id)=? OR LOWER(name)=? OR LOWER(name) LIKE ?",
                     (team.lower(), team.lower(), f"%{team.lower()}%")).fetchone()
    return r[0] if r else None


def get_preferences(conn, user_id: str | None) -> dict:
    if not user_id:
        return {}
    return {r["pref_key"]: r["pref_value"] for r in conn.execute(
        "SELECT pref_key, pref_value FROM user_prefs WHERE user_id=?", (user_id,))}


def set_preference(conn, user_id: str, key: str, value: str) -> dict:
    """Private per-user display preference (never visible to other users)."""
    memory._user(conn, user_id)
    if key not in ("sort_order", "cost_unit"):
        raise ValueError("key must be 'sort_order' (e.g. 'area,well_id') or 'cost_unit' ('thousands' or 'dollars')")
    conn.execute("INSERT OR REPLACE INTO user_prefs VALUES (?,?,?,?)",
                 (user_id, key, value.strip().lower(), memory._stamp(conn)))
    conn.commit()
    return {"saved": True, "private_to_user": user_id, "key": key, "value": value.strip().lower(),
            "note": "Applies only to this user's later answers; no other user sees or is affected by it."}


def _recall_params(conn) -> dict:
    d = next((x for x in _sop_docs(conn).values() if "recall" in x["text"].lower() and "LOT-" in x["text"]), None)
    if not d:
        raise ValueError("no supplier recall notice found in the documents")
    t = d["text"]
    sec = t.split("## What is recalled", 1)[-1].split("## The problem", 1)[0]
    recalled = re.split(r"[Pp]umps from all other lots", sec)[0]
    return {"model": (re.search(r"(Highline\s+[A-Z]+-\d+)", recalled) or re.search(r"(\w+ [A-Z]+-\d+)", recalled)).group(1),
            "lots": sorted(set(re.findall(r"LOT-[\w-]+", recalled))),
            "ref": (re.search(r"\*\*Reference:\*\*\s*(\S+)", t) or [None, None])[1],
            "notice_date": None,
            "deadline": (_d(re.search(r"by " + _DATE_RE + r"\)", t).group(0)).isoformat()
                         if re.search(r"by " + _DATE_RE + r"\)", t) else None), "chunk": d["chunks"][0]}


def recalled_pumps(conn, scope: str = "all", user_id: str | None = None) -> dict:
    p = _recall_params(conn)
    prefs = get_preferences(conn, user_id)
    team = None
    sc = scope.strip().lower()
    if sc in ("my area", "my areas", "my team", "mine", "my region"):
        team = _user_team(conn, user_id)
        if not team:
            raise ValueError("cannot resolve 'my area' without a signed-in user")
        ids = [r[0] for r in conn.execute("SELECT asset_id FROM wells") if effective_team(conn, r[0]) == team]
    else:
        ids = metrics.resolve_scope(conn, scope)["wells"]
    idset = set(ids)
    cur: dict[str, dict] = {}
    for r in conn.execute(
            """SELECT m.asset_id, m.maintenance_id, m.started_at, m.cost_total, m.issue, e.pump_model, e.pump_lot, e.pump_serial,
                      x.cost_original, x.cost_revision_reason
               FROM report_extra e JOIN maintenance m ON m.maintenance_id=e.maintenance_id
               LEFT JOIN maintenance_extra x ON x.maintenance_id=m.maintenance_id
               WHERE e.pump_model IS NOT NULL ORDER BY m.started_at"""):
        cur[r["asset_id"]] = dict(r)
    claims: dict[str, list] = {}
    for wid in idset:
        for f in memory.facts_for_well(conn, wid, include_disputed=True):
            if f["payload"].get("kind") == "pump_replaced" and f["scope_type"] == "well":
                claims.setdefault(wid, []).append(f)
    rows, ev = [], [p["chunk"]]
    for wid in sorted(idset):
        c = cur.get(wid)
        if not c or c["pump_model"] != p["model"] or c["pump_lot"] not in p["lots"]:
            continue
        w = conn.execute("SELECT area_id, recorded_status FROM wells WHERE asset_id=?", (wid,)).fetchone()
        x = conn.execute("SELECT status FROM well_extra WHERE asset_id=?", (wid,)).fetchone()
        cl = [{"fact_id": f["fact_id"], "by": f["author_name"], "text": f["text"], "status": f["status"],
               "second_hand": f["payload"].get("second_hand"), "heard_from": f["payload"].get("heard_from"),
               "created_at": f["created_at"]} for f in claims.get(wid, [])]
        row = {"well_id": wid, "area": _area_name(conn, w["area_id"]), "area_id": w["area_id"], "pump_model": c["pump_model"],
               "lot": c["pump_lot"], "serial": c["pump_serial"], "installed_job": c["maintenance_id"],
               "installed_on": c["started_at"][:10], "install_cost_usd": c["cost_total"],
               "install_cost_original_usd": c["cost_original"], "cost_revision_reason": c["cost_revision_reason"],
               "recorded_status": w["recorded_status"], "well_record_status": x[0] if x else None,
               "user_claims": cl, "data_source": "maintenance report " + c["maintenance_id"] + " (Parts installed)"}
        rows.append(row)
        ev += [c["maintenance_id"]] + [f["fact_id"] for f in cl]
    applied = {}
    if prefs.get("sort_order"):
        keys = [k.strip() for k in re.split(r"[,>]| then ", prefs["sort_order"]) if k.strip()]
        keymap = {"area": "area", "well": "well_id", "well_id": "well_id", "well id": "well_id"}
        sk = [keymap.get(k, k) for k in keys]
        rows.sort(key=lambda r: tuple(str(r.get(k, "")) for k in sk))
        applied["sort_order"] = prefs["sort_order"]
    if prefs.get("cost_unit") == "thousands":
        for r in rows:
            for k in ("install_cost_usd", "install_cost_original_usd"):
                if r[k] is not None:
                    r[k + "_display"] = f"${r[k] / 1000:.1f}k"
        applied["cost_unit"] = "thousands"
    by_area: dict[str, int] = {}
    for r in rows:
        by_area[r["area"]] = by_area.get(r["area"], 0) + 1
    return {"recall": {k: p[k] for k in ("ref", "model", "lots", "deadline")},
            "rule": "only the pump currently in the well counts: the latest 'Parts installed' per well; a later "
                    "replacement removes the well. User claims of replacement are NOT in the data and do not remove a well",
            "scope": scope, "team_filter": team, "count": len(rows), "by_area": by_area,
            "with_unverified_claims": sum(1 for r in rows if r["user_claims"]), "preferences_applied": applied,
            "rows": rows, "evidence_ids": ev}


# ---- teams ----------------------------------------------------------------------------------------
def team_info(conn, team: str) -> dict:
    tid = _team_ids(conn, team) or (conn.execute("SELECT team_id FROM areas WHERE LOWER(name)=?", (team.lower(),)).fetchone() or [None])[0]
    if not tid:
        raise ValueError(f"unknown team {team!r}")
    t = conn.execute("SELECT * FROM teams WHERE team_id=?", (tid,)).fetchone()
    ws = [r[0] for r in conn.execute("SELECT asset_id FROM wells ORDER BY asset_id") if effective_team(conn, r[0]) == tid]
    plugged = conn.execute(f"SELECT COUNT(*) FROM well_extra WHERE status='plugged_and_abandoned' AND asset_id IN "
                           f"({','.join('?' * len(ws)) or 'NULL'})", ws).fetchone()[0]
    static = conn.execute("SELECT COUNT(*) FROM wells WHERE team_id=?", (tid,)).fetchone()[0]
    moved = [dict(r) for r in conn.execute("SELECT asset_id, team_id, from_date, to_date FROM well_team_history "
                                           "WHERE team_id=? OR asset_id IN (SELECT asset_id FROM well_team_history WHERE team_id=?) "
                                           "ORDER BY asset_id, from_date", (tid, tid))]
    areas = [r["name"] for r in conn.execute("SELECT name FROM areas WHERE team_id=?", (tid,))]
    return {"team_id": tid, "team_name": t["name"], "lead": _uname(conn, t["lead_user_id"]), "lead_user_id": t["lead_user_id"],
            "wells_run_today": len(ws), "of_which_plugged_and_abandoned": plugged,
            "as_of": config.today().isoformat(), "areas": areas,
            "wells_by_static_team_column": static, "team_history_rows": moved[:60],
            "note": "well count uses the team history in force today (wells that moved teams are counted with their current "
                    "team); the static metadata column differs only for wells that moved",
            "evidence_ids": [tid, t["lead_user_id"]]}


# ---- OPS-5 cold weather -------------------------------------------------------------------------
def freeze_protection_check(conn, since: str | None = None, until: str | None = None) -> dict:
    d = next((x for x in _sop_docs(conn).values() if "OPS-5" in x["text"]), None)
    if not d:
        raise ValueError("no OPS-5 found in SOPs")
    sec = d["text"].split("## OPS-5", 1)[-1].split("## OPS-6", 1)[0]
    win = re.search(r"From (\d+)\s+([A-Za-z]{3}) to (\d+)\s+([A-Za-z]{3})", sec)
    thr = float(re.search(r"at or below \*\*[−-]?(\d+)", sec).group(1)) * -1
    days = int(re.search(r"within \*\*(\d+) days", sec).group(1))
    key = re.search(r'contains "([^"]+)"', sec).group(1)
    m1, m2 = _MONTHS[win.group(2).lower()], _MONTHS[win.group(4).lower()]
    d1, d2 = int(win.group(1)), int(win.group(3))
    today = config.today()
    lo = date.fromisoformat(since) if since else date(today.year - 1, 1, 1)
    hi = date.fromisoformat(until) if until else today

    def in_win(dt: date) -> bool:
        md = (dt.month, dt.day)
        return md >= (m1, d1) or md <= (m2, d2) if (m1, d1) > (m2, d2) else (m1, d1) <= md <= (m2, d2)

    res, ev = [], [d["chunks"][0]]
    for i in conn.execute("""SELECT inspection_id, asset_id, inspected_at, inspected_date, ambient_temp_c, delta_c, w.area_id
                             FROM inspection_effective JOIN wells w USING(asset_id)
                             WHERE ambient_temp_c <= ? AND producing=1 AND inspected_date >= ? AND inspected_date <= ?
                             ORDER BY asset_id, inspected_at""", (thr, lo.isoformat(), hi.isoformat())):
        if not in_win(date.fromisoformat(i["inspected_date"])):
            continue
        end = (_ts(i["inspected_at"]) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        job = conn.execute("SELECT maintenance_id, started_at FROM maintenance WHERE asset_id=? AND issue LIKE ? AND "
                           "started_at >= ? AND started_at <= ? ORDER BY started_at LIMIT 1",
                           (i["asset_id"], f"%{key}%", i["inspected_at"], end)).fetchone()
        due_passed = _ts(end).date() < today
        st = "Done" if job else ("Missed" if due_passed else "Open")
        res.append({"well_id": i["asset_id"], "area": _area_name(conn, i["area_id"]), "inspection_id": i["inspection_id"],
                    "inspected_at": i["inspected_at"], "ambient_c": i["ambient_temp_c"], "check_due_by": end,
                    "check_job": job["maintenance_id"] if job else None, "status": st})
    missed = [r for r in res if r["status"] == "Missed"]
    return {"rule": {"sop": "OPS-5", "season": f"{d1} {win.group(2)} to {d2} {win.group(4)}", "ambient_at_or_below_c": thr,
                     "check_within_days": days, "check_issue_contains": key,
                     "applies_to": "producing wells only (cold reading on a non-producing well needs no check)"},
            "period": [lo.isoformat(), hi.isoformat()], "cold_producing_readings": len(res), "missed": len(missed),
            "missed_by_month": _by_month(missed), "missed_examples": missed[:15], "done": sum(r["status"] == "Done" for r in res),
            "evidence_ids": ev + [r["inspection_id"] for r in missed[:15]]}


def _by_month(rows):
    out: dict[str, int] = {}
    for r in rows:
        out[r["inspected_at"][:7]] = out.get(r["inspected_at"][:7], 0) + 1
    return dict(sorted(out.items()))


# ---- repair delay reasons (Q10) ----------------------------------------------------------------
def _reason_class(text: str) -> str:
    t = text.lower()
    if "back-order" in t or "part" in t:
        return "Parts"
    if "contractor" in t:
        return "Contractors"
    if "rain" in t or "road" in t or "weather" in t:
        return "Weather"
    if "crew" in t or "rig" in t:
        return "Crews"
    return "Other"


def repair_delay_reasons(conn, year: int | None = None, scope: str = "all") -> dict:
    ids = set(metrics.resolve_scope(conn, scope)["wells"])
    rows = conn.execute(
        """SELECT m.maintenance_id, m.asset_id, m.started_at, w.area_id, e.delay_days, e.delay_reason, f.in_progress
           FROM report_extra e JOIN maintenance m ON m.maintenance_id=e.maintenance_id JOIN wells w ON w.asset_id=m.asset_id
           LEFT JOIN report_facts f ON f.maintenance_id=m.maintenance_id
           WHERE e.delay_days IS NOT NULL""").fetchall()
    data: dict[str, dict[str, list]] = {}
    ev = []
    for r in rows:
        if r["asset_id"] not in ids or (year is not None and not r["started_at"].startswith(str(year))):
            continue
        a = _area_name(conn, r["area_id"])
        data.setdefault(a, {}).setdefault(_reason_class(r["delay_reason"]), []).append(r["delay_days"])
        ev.append(r["maintenance_id"])
    reasons = sorted({k for a in data.values() for k in a})
    fleet = {k: sum(len(a.get(k, [])) for a in data.values()) for k in reasons}
    ftot = sum(fleet.values()) or 1
    table, series, stand = [], {k: [] for k in reasons}, []
    for area in sorted(data):
        tot = sum(len(v) for v in data[area].values())
        for k in reasons:
            v = data[area].get(k, [])
            share = round(100 * len(v) / tot, 1)
            table.append({"area": area, "reason": k, "jobs": len(v), "share_pct": share,
                          "typical_extra_days": round(statistics.median(v), 1) if v else None})
            series[k].append({"x": area, "y": share})
            stand.append({"area": area, "reason": k, "share_pct": share, "fleet_share_pct": round(100 * fleet[k] / ftot, 1),
                          "diff_pts": round(share - 100 * fleet[k] / ftot, 1), "jobs_in_area": tot})
    stand.sort(key=lambda r: -abs(r["diff_pts"]))
    allv = [x for a in data.values() for v in a.values() for x in v]
    return {"year": year, "scope": scope, "jobs_with_delay": ftot if data else 0,
            "basis": "jobs started in the period whose maintenance report states the well stayed shut in longer than the "
                     "repair itself, with the stated reason grouped into Parts / Contractors / Crews / Weather",
            "fleet_share_pct": {k: round(100 * v / ftot, 1) for k, v in fleet.items()},
            "fleet_typical_extra_days": round(statistics.median(allv), 1) if allv else None,
            "table": table, "stacked_bar_series": [{"name": k, "points": series[k]} for k in reasons],
            "largest_deviations_from_fleet": stand[:6], "evidence_ids": ev[:40]}


# ---- area temperatures (Q11) -----------------------------------------------------------------------
def area_temperature_comparison(conn, period: str | None = None, start: str | None = None, end: str | None = None) -> dict:
    if period or start or end:
        s, e = metrics.resolve_range(period, start, end)
    else:
        e = config.today()
        s = metrics.add_months(e, -12)
    s, e = str(s)[:10], str(e)[:10]
    rows = conn.execute(
        """SELECT w.area_id, SUBSTR(i.inspected_date,1,7) mo, AVG(i.ambient_temp_c) amb, AVG(i.wellhead_temp_c) wh,
                  AVG(CASE WHEN i.producing=1 THEN i.wellhead_temp_c END) wh_prod, AVG(CASE WHEN i.producing=1 THEN i.delta_c END) dl,
                  COUNT(*) n FROM inspection_effective i JOIN wells w ON w.asset_id=i.asset_id
           WHERE i.inspected_date >= ? AND i.inspected_date <= ? GROUP BY 1,2""", (s, e)).fetchall()
    months = sorted({r["mo"] for r in rows})
    areas = sorted({r["area_id"] for r in rows})
    res = {}
    for metric in ("amb", "wh", "wh_prod", "dl"):
        mat = {a: {r["mo"]: r[metric] for r in rows if r["area_id"] == a and r[metric] is not None} for a in areas}
        fm = {m: statistics.mean([mat[a][m] for a in areas if m in mat[a]]) for m in months if any(m in mat[a] for a in areas)}
        summary = []
        for a in areas:
            vals = list(mat[a].values())
            above = sum(1 for m, v in mat[a].items() if v > fm[m])
            summary.append({"area": _area_name(conn, a), "area_id": a, "mean_c": round(statistics.mean(vals), 2) if vals else None,
                            "months_above_fleet_mean": above, "months": len(mat[a])})
        summary.sort(key=lambda x: -(x["mean_c"] or -99))
        res[{"amb": "ambient_air_temp_c", "wh": "wellhead_temp_c_all_inspections", "wh_prod": "wellhead_temp_c_producing_only",
             "dl": "wellhead_minus_ambient_c_producing_only"}[metric]] = {
            "ranking": summary, "monthly": {_area_name(conn, a): {m: round(v, 2) for m, v in mat[a].items()} for a in areas}}
    return {"period": [s, e], "assumption_note": "'hotter' is ambiguous: ambient (weather) and wellhead (the well itself) can "
            "rank differently. Both are reported; 'months_above_fleet_mean' measures consistency.",
            "metrics": res, "evidence_ids": ["inspection_effective (all inspections in period)"]}


# ---- operator-note complaints (Q7) -------------------------------------------------------------------
def operator_complaints(conn, since: str | None = None, top_n: int = 10) -> dict:
    today = config.today()
    s = date.fromisoformat(since) if since else metrics.add_months(today, -1)
    notes: dict[str, list] = {}
    for n in conn.execute("SELECT note_id, asset_id, created_at, text FROM notes WHERE note_type='operator_note' AND "
                          "SUBSTR(created_at,1,10) >= ? ORDER BY created_at", (s.isoformat(),)):
        if NEUTRAL_RE.search(n["text"]) or GAS_RE.search(n["text"]) or SHUT_RE.search(n["text"]) \
                or RESTART_RE.search(n["text"]) or BYPASS_RE.search(n["text"]):
            continue
        notes.setdefault(n["asset_id"], []).append(dict(n))
    cand = []
    for wid, ns in notes.items():
        last = ns[-1]["created_at"]
        rep = conn.execute("SELECT maintenance_id, started_at, issue FROM maintenance WHERE asset_id=? AND "
                           "maintenance_type='corrective' AND started_at > ? ORDER BY started_at LIMIT 1", (wid, ns[0]["created_at"])).fetchone()
        if rep:
            continue
        lastrep = conn.execute("SELECT maintenance_id, started_at, issue FROM maintenance WHERE asset_id=? ORDER BY started_at DESC "
                               "LIMIT 1", (wid,)).fetchone()
        lastcorr = conn.execute("SELECT maintenance_id, started_at, issue FROM maintenance WHERE asset_id=? AND "
                                "maintenance_type='corrective' ORDER BY started_at DESC LIMIT 1", (wid,)).fetchone()
        lo = metrics.add_months(today, -3).isoformat()
        down = conn.execute("SELECT COUNT(*) FROM inspection_effective WHERE asset_id=? AND producing=0 AND inspected_date>=?",
                            (wid, lo)).fetchone()[0]
        corr12 = conn.execute("SELECT COUNT(*) FROM maintenance WHERE asset_id=? AND maintenance_type='corrective' AND "
                              "started_at >= ?", (wid, metrics.add_months(today, -12).isoformat())).fetchone()[0]
        ins = conn.execute("SELECT inspection_id, delta_c, producing_min_delta_c, producing FROM inspection_effective WHERE "
                           "asset_id=? ORDER BY inspected_at DESC LIMIT 1", (wid,)).fetchone()
        margin = (ins["delta_c"] - ins["producing_min_delta_c"]) if ins and ins["delta_c"] is not None and ins["producing_min_delta_c"] is not None else None
        signs = []
        if down:
            signs.append(f"{down} non-producing inspections in last 3 months")
        if corr12:
            signs.append(f"{corr12} corrective repairs in last 12 months")
        if margin is not None and margin < 5 and ins["producing"]:
            signs.append(f"latest producing margin only {margin:.1f} C above threshold ({ins['inspection_id']})")
        w = conn.execute("SELECT area_id FROM wells WHERE asset_id=?", (wid,)).fetchone()
        score = len(ns) * 2 + down * 1.5 + corr12 + (1 if margin is not None and margin < 5 and ins["producing"] else 0)
        cand.append({"well_id": wid, "area": _area_name(conn, w["area_id"]), "score": round(score, 1),
                     "complaint_count": len(ns), "notes": [{"id": n["note_id"], "date": n["created_at"], "quote": n["text"]} for n in ns],
                     "other_warning_signs": signs or ["none beyond the notes"],
                     "last_repair": dict(lastrep) if lastrep else None, "last_corrective_repair": dict(lastcorr) if lastcorr else None,
                     "evidence_ids": [n["note_id"] for n in ns] + ([lastrep["maintenance_id"]] if lastrep else [])})
    cand.sort(key=lambda r: (-r["score"], -r["complaint_count"], r["well_id"]))
    for i, r in enumerate(cand, 1):
        r["rank"] = i
    return {"since": s.isoformat(), "today": today.isoformat(),
            "method": "wells with operator notes since the date that are not routine/neutral and not shut-in/restart/gas/bypass "
                      "notes, and with no corrective repair started after the first such note. Score (heuristic, transparent) = "
                      "2 per complaint note + 1.5 per non-producing inspection in 3 months + 1 per corrective repair in 12 months "
                      "+ 1 if the latest producing margin is < 5 C",
            "wells_with_complaints_and_no_repair_since": len(cand), "top": cand[:top_n],
            "evidence_ids": [e for r in cand[:top_n] for e in r["evidence_ids"]]}


# ---- data coverage (Q1/Q2) ---------------------------------------------------------------------------
def data_inventory(conn) -> dict:
    """What fields the dataset really contains, so questions about absent data (pressure, oil volumes) are answered honestly."""
    cols = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            for t in ("wells", "inspections", "maintenance", "regulatory_events", "notes", "report_facts", "report_extra")}
    return {"fields_available": cols, "not_in_dataset": ["reservoir or wellhead pressure readings", "oil/gas production volumes "
            "(barrels produced)", "flow rates"],
            "proxies": ["producing/not-producing status from wellhead-minus-ambient temperature at weekly inspections",
                        "uptime share = producing inspections / all inspections", "downtime days (7 per non-producing inspection)"],
            "instruction": "If the question needs data not in the dataset, say so plainly, do not fabricate or silently substitute; "
                           "offer the proxies and ask whether to use one."}

