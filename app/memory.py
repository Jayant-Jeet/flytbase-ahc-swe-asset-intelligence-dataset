"""Shared facts, private conversation memory, corrections and conflict detection.

Everything here is deterministic code. The model only decides which tool to call and passes structured
arguments (state, dates, field, value); comparisons with the data happen here.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from . import compliance, config

CORRECTABLE = {
    "wellhead_temp_c": "REAL", "ambient_temp_c": "REAL", "spill_detected": "BOOL", "spill_est_volume_bbl": "REAL"}
STATES = {"producing": "producing", "running": "producing", "online": "producing",
          "shut_in": "not_producing", "not_producing": "not_producing", "down": "not_producing", "offline": "not_producing"}

NOT_RUNNING_RE = re.compile(r"not (moving|running|stroking|operating|pumping)|stopped|idle|seized|tripped|"
                            r"no (flow|movement|stroke)|shut[\s-]?in|\bdown\b|not working", re.I)
RUNNING_RE = re.compile(r"(running|operating|pumping|moving|stroking) (normally|fine|well|smoothly)|"
                        r"normal (operation|stroke)|working fine", re.I)
SPILL_WORDS_RE = re.compile(r"sheen|crude|oil on|stain|puddle|leak|spill|seep", re.I)
NO_SPILL_RE = re.compile(r"rain\s?water|produced water|water only|no oil|no spill|dry|clean", re.I)


# ---- clock, ids ------------------------------------------------------------------------------

def logical_today(conn) -> date:
    """Conversation dating: the configured TODAY plus a demo day offset ("next day" button / eval harness)."""
    r = conn.execute("SELECT value FROM kv WHERE key='day_offset'").fetchone()
    return config.today() + timedelta(days=int(r[0]) if r else 0)


def advance_day(conn, days: int = 1) -> str:
    cur = conn.execute("SELECT value FROM kv WHERE key='day_offset'").fetchone()
    conn.execute("INSERT OR REPLACE INTO kv VALUES ('day_offset', ?)", (str(int(cur[0]) + days if cur else days),))
    conn.commit()
    return logical_today(conn).isoformat()


def _stamp(conn) -> str:
    return f"{logical_today(conn).isoformat()}T{datetime.now():%H:%M:%S.%f}Z"


def _user(conn, user_id):
    if not user_id:
        raise ValueError("no user is selected; this action needs a signed-in user")
    r = conn.execute("SELECT user_id, name, role FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        raise ValueError(f"unknown user {user_id!r}")
    return r


def _uname(conn, uid):
    r = conn.execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r else uid


def reset(conn) -> None:
    """Clear all memory (facts, user corrections, conversations). Dataset rows are untouched."""
    for t in ("facts", "conversations", "messages", "kv"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM corrections WHERE source='user'")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('facts','messages')")
    conn.commit()


# ---- facts (append-only) ---------------------------------------------------------------------

def add_fact(conn, scope_type: str, scope_id: str, text: str, payload: dict | None, author: str, status: str,
             supersedes_id: str | None = None, reason: str | None = None, original_value=None) -> str:
    n = (conn.execute("SELECT COALESCE(MAX(seq), 0) FROM facts").fetchone()[0]) + 1
    fid = f"FACT-{n:04d}"
    conn.execute("INSERT INTO facts (id, scope_type, scope_id, text, payload, author_user_id, created_at, status, "
                 "supersedes_id, reason, original_value) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (fid, scope_type, scope_id, text, json.dumps(payload or {}), author, _stamp(conn), status,
                  supersedes_id, reason, None if original_value is None else str(original_value)))
    return fid


def _fact_view(conn, r) -> dict:
    return {"fact_id": r["id"], "scope_type": r["scope_type"], "scope_id": r["scope_id"], "text": r["text"],
            "payload": json.loads(r["payload"] or "{}"), "author_user_id": r["author_user_id"],
            "author_name": _uname(conn, r["author_user_id"]), "created_at": r["created_at"], "status": r["status"],
            "supersedes_id": r["supersedes_id"], "reason": r["reason"], "original_value": r["original_value"]}


def facts_for_well(conn, well_id: str, include_disputed: bool = False) -> list[dict]:
    w = conn.execute("SELECT well_type FROM wells WHERE asset_id=?", (well_id,)).fetchone()
    wt = w["well_type"] if w else None
    rows = conn.execute(
        """SELECT * FROM facts_current WHERE (scope_type='well' AND scope_id=?) OR (scope_type='well_type' AND scope_id=?)
           OR scope_type='global' ORDER BY seq""", (well_id, wt)).fetchall()
    return [_fact_view(conn, r) for r in rows if include_disputed or r["status"] != "disputed"]


def active_shutdown_facts(conn, well_id: str) -> list[dict]:
    """User-stated shut-ins whose window has not ended as of TODAY."""
    t = config.today().isoformat()
    out = []
    for f in facts_for_well(conn, well_id):
        p = f["payload"]
        if f["scope_type"] == "well" and p.get("kind") == "well_state" and p.get("state") == "not_producing" \
                and (not p.get("until") or p["until"] >= t):
            out.append(f)
    return out


def _interval(p: dict) -> tuple[str, str]:
    return p.get("since") or "0000-00-00", p.get("until") or "9999-12-31"


def _overlap(a: dict, b: dict) -> bool:
    (a0, a1), (b0, b1) = _interval(a), _interval(b)
    return a0 <= b1 and b0 <= a1


def _latest_state(conn, wid: str):
    return conn.execute("SELECT inspection_id, inspected_at, inspected_date, producing, delta_c, producing_min_delta_c, "
                        "inspector_user_id FROM inspection_effective WHERE asset_id=? ORDER BY inspected_at DESC LIMIT 1",
                        (wid,)).fetchone()


def _resolve_well(conn, well_id: str) -> str:
    from . import metrics
    sc = metrics.resolve_scope(conn, well_id)
    if sc["kind"] != "well":
        raise ValueError(f"{well_id!r} is not a known well")
    return sc["id"]


def _norm_date(s: str | None, field: str) -> str | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        raise ValueError(f"{field} must be YYYY-MM-DD") from None


def remember_fact(conn, user_id: str, scope_type: str, scope_id: str, text: str, state: str | None = None,
                  since: str | None = None, until: str | None = None, duration_days: int | None = None,
                  rule_key: str | None = None, rule_value: float | None = None, reason: str | None = None) -> dict:
    """Store a user claim as shared knowledge. Claims that disagree with the data or other facts are kept as
    'unconfirmed' and returned with both sources so the user can be asked to confirm."""
    u = _user(conn, user_id)
    if scope_type not in ("well", "well_type", "global"):
        raise ValueError("scope_type must be well, well_type or global")
    if not text or not text.strip():
        raise ValueError("text is required")
    payload: dict = {"kind": "note"}
    conflicts: list[dict] = []
    wid = None
    if scope_type == "well":
        wid = scope_id = _resolve_well(conn, scope_id)
    elif scope_type == "well_type":
        if not conn.execute("SELECT 1 FROM wells WHERE well_type=? LIMIT 1", (scope_id,)).fetchone():
            raise ValueError(f"unknown well type {scope_id!r}")

    if rule_key is not None:
        if scope_type != "well_type":
            raise ValueError("a rule override must have scope_type 'well_type'")
        if rule_value is None:
            raise ValueError("rule_value is required with rule_key")
        manual = conn.execute("SELECT value_num, unit, source, section FROM rules WHERE rule_key=? AND "
                              "scope_type='well_type' AND scope_id=?", (rule_key, scope_id)).fetchone()
        if not manual:
            known = [r[0] for r in conn.execute("SELECT DISTINCT rule_key FROM rules WHERE scope_type='well_type'")]
            raise ValueError(f"no well-type rule {rule_key!r} exists to override; known: {known}")
        prev = conn.execute("SELECT id FROM facts_current WHERE scope_type='well_type' AND scope_id=? AND "
                            "json_extract(payload,'$.rule_key')=? AND status='confirmed'", (scope_id, rule_key)).fetchone()
        payload = {"kind": "rule", "rule_key": rule_key, "value": float(rule_value), "unit": manual["unit"],
                   "manual_value": manual["value_num"], "manual_source": f"{manual['source']}#{manual['section']}"}
        fid = add_fact(conn, "well_type", scope_id, text, payload, user_id, "confirmed",
                       supersedes_id=prev[0] if prev else None, reason=reason, original_value=manual["value_num"])
        conn.commit()
        return {"fact_id": fid, "status": "confirmed", "kind": "rule_override", "well_type": scope_id,
                "rule_key": rule_key, "new_value": float(rule_value), "manual_value": manual["value_num"],
                "applies_to": f"well type {scope_id} only", "recorded_by": u["name"],
                "note": "From now on answers for this well type follow this rule instead of the manual.",
                "evidence_ids": [fid]}

    data_shows = None
    if state is not None:
        norm = STATES.get(state.lower().replace(" ", "_"))
        if not norm:
            raise ValueError(f"state must be one of {sorted(set(STATES))}")
        if scope_type != "well":
            raise ValueError("a well state claim needs scope_type 'well'")
        since, until = _norm_date(since, "since"), _norm_date(until, "until")
        dated = since is not None
        if not since:
            since = config.today().isoformat()
        if duration_days and not until:
            until = (date.fromisoformat(since) + timedelta(days=int(duration_days))).isoformat()
        payload = {"kind": "well_state", "state": norm, "since": since, "until": until, "dated": dated}
        conflicts, data_shows = _state_conflicts(conn, wid, payload, user_id)
    elif since or until:
        payload.update(since=_norm_date(since, "since"), until=_norm_date(until, "until"))

    status = "unconfirmed" if conflicts else "confirmed"
    supersedes = None
    if payload.get("kind") == "well_state":
        mine = [f for f in facts_for_well(conn, wid) if f["author_user_id"] == user_id
                and f["payload"].get("kind") == "well_state" and f["scope_type"] == "well"]
        supersedes = mine[-1]["fact_id"] if mine and _overlap(mine[-1]["payload"], payload) else None
    fid = add_fact(conn, scope_type, scope_id, text, payload, user_id, status, supersedes_id=supersedes, reason=reason)
    conn.commit()
    out = {"fact_id": fid, "status": status, "scope_type": scope_type, "scope_id": scope_id, "payload": payload,
           "recorded_by": u["name"], "conflicts": conflicts, "data_shows": data_shows,
           "needs_confirmation": bool(conflicts),
           "evidence_ids": [fid] + [c["id"] for c in conflicts if c.get("id")]}
    if conflicts:
        out["instruction"] = ("Do not accept or reject the claim. Say it is saved as unconfirmed, state what the data and "
                              "the other source(s) show (cite their ids), and ask the user to confirm or withdraw "
                              "(confirm_fact).")
    elif data_shows and data_shows.get("agrees") is True:
        out["supported_by_data"] = True
    return out


def _state_conflicts(conn, wid: str, claim: dict, user_id: str):
    conflicts: list[dict] = []
    want = claim["state"]
    lat = _latest_state(conn, wid)
    shows = None
    if lat and lat["producing"] is not None:
        actual = "producing" if lat["producing"] else "not_producing"
        shows = {"inspection_id": lat["inspection_id"], "inspected_date": lat["inspected_date"], "state": actual,
                 "delta_c": lat["delta_c"], "threshold_delta_c": lat["producing_min_delta_c"],
                 "inspector": _uname(conn, lat["inspector_user_id"])}
        comparable = (not claim.get("dated")) or claim["since"] <= lat["inspected_date"]
        shows["comparable"] = comparable
        shows["agrees"] = (actual == want) if comparable else None
        if comparable and actual != want:
            conflicts.append({"source": "inspection", "id": lat["inspection_id"],
                              "detail": f"inspection on {lat['inspected_date']} shows the well {actual.replace('_', ' ')} "
                                        f"(wellhead {lat['delta_c']} C above air, threshold {lat['producing_min_delta_c']} C)"})
    t = config.today().isoformat()
    if want == "producing":
        for n in conn.execute("SELECT note_id, author_user_id, text, valid_from, valid_to FROM notes WHERE asset_id=? "
                              "AND note_type!='correction' AND valid_from IS NOT NULL", (wid,)):
            if re.search(r"shut[\s-]?in|shut\s?down|workover|offline|out of service", n["text"] or "", re.I) \
                    and _overlap(claim, {"since": n["valid_from"][:10], "until": (n["valid_to"] or "")[:10] or None}):
                conflicts.append({"source": "note", "id": n["note_id"], "author_user_id": n["author_user_id"],
                                  "detail": f"note says: {n['text']} (valid {n['valid_from'][:10]} to {(n['valid_to'] or 'open')[:10]})"})
    for f in facts_for_well(conn, wid):
        p = f["payload"]
        if f["scope_type"] == "well" and p.get("kind") == "well_state" and f["author_user_id"] != user_id \
                and p.get("state") != want and _overlap(claim, p):
            conflicts.append({"source": "fact", "id": f["fact_id"], "author_user_id": f["author_user_id"],
                              "author_name": f["author_name"], "status": f["status"],
                              "detail": f"{f['author_name']} stated: {f['text']}"})
    return conflicts, shows


def confirm_fact(conn, user_id: str, fact_id: str, decision: str, reason: str | None = None) -> dict:
    """decision: 'confirm' keeps the fact, 'withdraw' marks it disputed. Appends a new row; history is kept."""
    u = _user(conn, user_id)
    if decision not in ("confirm", "withdraw"):
        raise ValueError("decision must be 'confirm' or 'withdraw'")
    r = conn.execute("SELECT * FROM facts_current WHERE id=?", (fact_id,)).fetchone()
    if not r:
        raise ValueError(f"no current fact {fact_id!r}")
    status = "confirmed" if decision == "confirm" else "disputed"
    new = add_fact(conn, r["scope_type"], r["scope_id"], r["text"], json.loads(r["payload"] or "{}"), r["author_user_id"],
                   status, supersedes_id=fact_id, reason=reason or f"{decision} by {u['name']}",
                   original_value=r["original_value"])
    p = json.loads(r["payload"] or "{}")
    if p.get("kind") == "correction":
        conn.execute("UPDATE corrections SET status=? WHERE correction_id=?",
                     ("applied" if decision == "confirm" else "rejected", p["correction_id"]))
    conn.commit()
    return {"fact_id": new, "supersedes": fact_id, "status": status, "decided_by": u["name"], "evidence_ids": [new, fact_id]}


# ---- corrections -----------------------------------------------------------------------------

def _to_bool(v) -> int:
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "y"):
            return 1
        if s in ("0", "false", "no", "n"):
            return 0
        raise ValueError("spill_detected must be true or false")
    return 1 if v else 0


def _snapshot(conn, iid: str) -> dict:
    r = conn.execute("SELECT * FROM inspection_effective WHERE inspection_id=?", (iid,)).fetchone()
    return {"wellhead_temp_c": r["wellhead_temp_c"], "ambient_temp_c": r["ambient_temp_c"], "delta_c": r["delta_c"],
            "producing": None if r["producing"] is None else bool(r["producing"]),
            "spill_detected": bool(r["spill_detected"]), "spill_volume_bbl": r["spill_est_volume_bbl"],
            "spill_class": compliance.spill_class(conn, r["spill_est_volume_bbl"]) if r["spill_detected"] else None}


def record_correction(conn, user_id: str, field: str, new_value, reason: str, inspection_id: str | None = None,
                      well_id: str | None = None, date_: str | None = None) -> dict:
    """Overlay a corrected value on an inspection for everyone. The raw value is never changed."""
    u = _user(conn, user_id)
    if field not in CORRECTABLE:
        raise ValueError(f"field must be one of {sorted(CORRECTABLE)}")
    if not reason or not reason.strip():
        raise ValueError("a reason is required (why the original value is wrong)")
    if not inspection_id:
        if not (well_id and date_):
            raise ValueError("give inspection_id, or well_id and date")
        wid = _resolve_well(conn, well_id)
        rows = conn.execute("SELECT inspection_id FROM inspections WHERE asset_id=? AND inspected_date=? "
                            "ORDER BY inspected_at DESC", (wid, _norm_date(date_, "date")))
        rows = rows.fetchall()
        if not rows:
            raise ValueError(f"no inspection of {wid} on {date_}")
        inspection_id = rows[0][0]
    raw = conn.execute("SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)).fetchone()
    if not raw:
        raise ValueError(f"unknown inspection {inspection_id!r}")
    if well_id and _resolve_well(conn, well_id) != raw["asset_id"]:
        raise ValueError(f"{inspection_id} belongs to {raw['asset_id']}, not {well_id}")

    kind = CORRECTABLE[field]
    try:
        val = _to_bool(new_value) if kind == "BOOL" else float(new_value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} needs a numeric value") from None
    if kind == "REAL" and field == "spill_est_volume_bbl" and val < 0:
        raise ValueError("volume cannot be negative")
    if field.endswith("temp_c") and not -60 <= val <= 400:
        raise ValueError("temperature out of plausible range")

    wid = raw["asset_id"]
    before = _snapshot(conn, inspection_id)
    prior = conn.execute("SELECT * FROM corrections_active WHERE inspection_id=? AND field=?", (inspection_id, field)).fetchone()
    cid = f"COR-U{(conn.execute('SELECT COUNT(*) FROM corrections WHERE source=?', ('user',)).fetchone()[0]) + 1:04d}"
    original = {"wellhead_temp_c": raw["wellhead_temp_c"], "ambient_temp_c": raw["ambient_temp_c"],
                "spill_detected": raw["spill_detected"], "spill_est_volume_bbl": raw["spill_est_volume_bbl"]}[field]

    conflicts: list[dict] = []
    if prior and str(prior["new_value"]) != str(val if kind != "BOOL" else int(val)):
        conflicts.append({"source": "correction", "id": prior["correction_id"], "author_user_id": prior["author_user_id"],
                          "detail": f"overrides an existing correction to {prior['new_value']} ({prior['reason']})"})
    texts = [("inspection", inspection_id, raw["inspector_user_id"], raw["notes"])] + [
        ("note", n["note_id"], n["author_user_id"], n["text"]) for n in conn.execute(
            "SELECT note_id, author_user_id, text FROM notes WHERE inspection_id=? AND note_type!='correction'",
            (inspection_id,))]

    # write the overlay first so the "after" state comes from the same view every tool uses
    conn.execute("INSERT INTO corrections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, inspection_id, field, str(val if kind != "BOOL" else int(val)), str(original), "user", None,
                  user_id, _stamp(conn), reason, "applied", raw["dataset"]))
    after = _snapshot(conn, inspection_id)
    for src, sid, author, text in texts:
        if not text:
            continue
        if field.endswith("temp_c") and after["producing"] is not None:
            bad = (after["producing"] and NOT_RUNNING_RE.search(text)) or (not after["producing"] and RUNNING_RE.search(text))
        elif field == "spill_detected":
            bad = (val == 0 and SPILL_WORDS_RE.search(text) and not NO_SPILL_RE.search(text)) or \
                  (val == 1 and NO_SPILL_RE.search(text))
        else:
            bad = False
        if bad:
            conflicts.append({"source": src, "id": sid, "author_user_id": author, "author_name": _uname(conn, author),
                              "detail": f"{'inspector note' if src == 'inspection' else 'note'} on the same inspection "
                                        f"says: \"{text}\""})
    status = "unconfirmed" if conflicts else "confirmed"
    claim = f"{field} of {inspection_id} corrected from {original} to {val if kind != 'BOOL' else bool(val)}: {reason}"
    fid = add_fact(conn, "well", wid, claim, {"kind": "correction", "correction_id": cid, "inspection_id": inspection_id,
                                              "field": field, "new_value": val}, user_id, status, reason=reason,
                   original_value=original)
    conn.execute("UPDATE corrections SET source_ref=?, status=? WHERE correction_id=?",
                 (fid, "unconfirmed_applied" if conflicts else "applied", cid))
    conn.commit()

    return {"correction_id": cid, "fact_id": fid, "inspection_id": inspection_id, "well_id": wid, "field": field,
            "original_value": original, "previous_effective_value": before_value(before, field),
            "new_value": val if kind != "BOOL" else bool(val), "recorded_by": u["name"], "reason": reason,
            "status": status, "applied_to_all_users": True, "before": before, "after": after,
            "changes": {k: [before[k], after[k]] for k in before if before[k] != after[k]},
            "conflicts": conflicts, "needs_confirmation": bool(conflicts),
            "downstream": _downstream(conn, wid, inspection_id, raw["inspected_date"]),
            "instruction": (("Applied for everyone, but it disagrees with the source(s) in 'conflicts'. Show both and ask "
                             "the user to confirm (confirm_fact) or withdraw.") if conflicts else
                            "Applied for everyone; original value kept."),
            "evidence_ids": sorted({inspection_id, cid, fid} | {c["id"] for c in conflicts if c.get("id")})}


def before_value(snap: dict, field: str):
    return {"wellhead_temp_c": snap["wellhead_temp_c"], "ambient_temp_c": snap["ambient_temp_c"],
            "spill_detected": snap["spill_detected"], "spill_est_volume_bbl": snap["spill_volume_bbl"]}[field]


def _downstream(conn, wid: str, iid: str, day: str) -> dict:
    """Re-run SOP logic for the corrected inspection (reportability, notice, deadlines, outdated report class)."""
    c = compliance.compliance_check(conn, wid, day, config.today().isoformat())
    sp = [s for s in c.get("spills_in_period", []) if s["inspection_id"] == iid]
    rel = lambda x: x.get("spill") == iid or iid in x.get("evidence", [])  # noqa: E731
    return {"spill": sp[0] if sp else None,
            "breaches": [b for b in c["breaches"] if rel(b)],
            "open_actions": [b for b in c["open_actions"] if rel(b)],
            "rules_met": [b for b in c["rules_met"] if rel(b)]}


# ---- private memory --------------------------------------------------------------------------

def start_conversation(conn, user_id: str, title: str | None = None) -> str:
    _user(conn, user_id)
    n = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] + 1
    cid = f"CONV-{n:04d}"
    conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?)",
                 (cid, user_id, logical_today(conn).isoformat(), _stamp(conn), title))
    conn.commit()
    return cid


def _log(conn, conv_id, user_id, role, content=None, tool_name=None, tool_args=None, wells=None):
    conn.execute("INSERT INTO messages (conv_id, user_id, day, ts, role, content, tool_name, tool_args, wells_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (conv_id, user_id, logical_today(conn).isoformat(), _stamp(conn), role, content, tool_name,
                  json.dumps(tool_args) if tool_args is not None else None, json.dumps(wells) if wells else None))
    conn.commit()


def log_message(conn, conv_id: str, user_id: str, role: str, content: str) -> None:
    _log(conn, conv_id, user_id, role, content=content)


def extract_wells(obj, out: list | None = None) -> list[str]:
    """Well ids a tool result is about, in order of first appearance (code, not the model, decides)."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("asset_id", "well_id") and isinstance(v, str) and v.startswith("WELL-") and v not in out:
                out.append(v)
            elif isinstance(v, (dict, list)) and k not in ("evidence_ids", "evidence"):
                extract_wells(v, out)
    elif isinstance(obj, list):
        for v in obj:
            extract_wells(v, out)
    return out


def log_tool_call(conn, conv_id: str, user_id: str, name: str, args: dict, result: dict) -> None:
    _log(conn, conv_id, user_id, "tool", tool_name=name, tool_args=args, wells=extract_wells(result))


def recall(conn, user_id: str, when: str = "yesterday", topic: str | None = None, well_id: str | None = None) -> dict:
    """The caller's OWN past conversations (filtered by user_id in SQL) plus shared facts about a well."""
    _user(conn, user_id)
    today = logical_today(conn)
    when = (when or "yesterday").lower().replace(" ", "_")
    lo, hi = {"today": (today, today), "yesterday": (today - timedelta(days=1),) * 2,
              "last_week": (today - timedelta(days=7), today - timedelta(days=1)),
              "earlier": (date.min, today - timedelta(days=1)), "all": (date.min, today)}.get(when, (None, None))
    if lo is None:
        raise ValueError("when must be today, yesterday, last_week, earlier or all")
    convs = conn.execute("SELECT * FROM conversations WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY started_at",
                         (user_id, lo.isoformat(), hi.isoformat())).fetchall()
    out = []
    for c in convs:
        msgs = conn.execute("SELECT * FROM messages WHERE conv_id=? AND user_id=? ORDER BY msg_id", (c["conv_id"], user_id)).fetchall()
        qs = [m["content"] for m in msgs if m["role"] == "user"]
        if topic and not any(topic.lower() in (q or "").lower() for q in qs):
            continue
        answers, last_q = [], None
        for m in msgs:
            if m["role"] == "user":
                last_q = m["content"]
            elif m["role"] == "tool" and m["wells_json"]:
                answers.append({"question": last_q, "tool": m["tool_name"], "tool_args": json.loads(m["tool_args"] or "{}"),
                                "wells": json.loads(m["wells_json"])})
        wells: list[str] = []
        for a in answers:
            wells += [w for w in a["wells"] if w not in wells]
        out.append({"conversation_id": c["conv_id"], "date": c["day"], "user_questions": qs,
                    "results": answers, "wells_discussed": wells})
    res = {"user_id": user_id, "when": when, "found": bool(out), "conversations": out,
           "wells_discussed": list(dict.fromkeys(w for c in out for w in c["wells_discussed"])),
           "evidence_ids": [c["conversation_id"] for c in out]}
    if not out:
        res["message"] = f"No earlier conversation found for this user ({when}). Do not guess; say so and ask what they mean."
    if well_id:
        wid = _resolve_well(conn, well_id)
        res["shared_facts"] = facts_for_well(conn, wid)
        res["evidence_ids"] += [f["fact_id"] for f in res["shared_facts"]]
    return res
