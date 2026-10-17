"""Deterministic metrics: all counts, sums and date math live here, never in the model."""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import config


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    m += 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def last_n_months(n: int, today: date | None = None) -> tuple[str, str]:
    t = today or config.today()
    return add_months(t, -n).isoformat(), t.isoformat()


def last_n_days(n: int, today: date | None = None) -> tuple[str, str]:
    t = today or config.today()
    return (t - timedelta(days=n)).isoformat(), t.isoformat()


def year_range(year: int, today: date | None = None) -> tuple[str, str]:
    t = today or config.today()
    end = min(date(year, 12, 31), t)
    return date(year, 1, 1).isoformat(), end.isoformat()


def parse_period(text: str, today: date | None = None) -> tuple[str, str]:
    """Turn 'last_4_months', 'last 90 days', 'last 13 weeks', '2025', 'year_to_date', 'last year' into dates."""
    t = today or config.today()
    s = (text or "").lower().replace("_", " ").strip()
    if m := re.fullmatch(r"(?:in\s+)?((?:19|20)\d\d)", s):
        return year_range(int(m.group(1)), t)
    if s in ("year to date", "ytd", "this year", "current year"):
        return year_range(t.year, t)
    if s in ("last year", "previous year"):
        return year_range(t.year - 1, t)
    if m := re.search(r"(?:last|past|previous)\s+(\d+)\s+(day|week|month|year)s?", s):
        n, unit = int(m.group(1)), m.group(2)
        if unit == "day":
            return last_n_days(n, t)
        if unit == "week":
            return last_n_days(7 * n, t)
        return last_n_months(n * (12 if unit == "year" else 1), t)
    if s in ("today", "now", "current"):
        return t.isoformat(), t.isoformat()
    raise ValueError(f"cannot parse period {text!r}; pass start and end as YYYY-MM-DD")


def resolve_range(period: str | None, start: str | None, end: str | None) -> tuple[str, str]:
    if start and end:
        return start, end
    if period:
        return parse_period(period)
    raise ValueError("give either period (e.g. 'last_4_months', '2025') or start and end")


def resolve_scope(conn, scope: str) -> dict:
    """Scope is a well id, area id/name, or well type. Returns {kind, id, wells:[...]}."""
    s = (scope or "").strip()
    q = s.lower()
    if not q or q in ("all", "fleet", "field", "*"):
        rows = conn.execute("SELECT asset_id FROM wells ORDER BY asset_id").fetchall()
        return {"kind": "fleet", "id": "all", "wells": [r[0] for r in rows]}
    m = re.search(r"well[-\s]?0*(\d+)", q)
    if m:
        wid = conn.execute("SELECT asset_id FROM wells WHERE CAST(SUBSTR(asset_id, INSTR(asset_id,'-')+1) AS INTEGER)=?",
                           (int(m.group(1)),)).fetchone()
        if wid:
            return {"kind": "well", "id": wid[0], "wells": [wid[0]]}
    for sql, kind in (("SELECT asset_id FROM wells WHERE LOWER(asset_id)=?", "well"),
                      ("SELECT area_id FROM areas WHERE LOWER(area_id)=? OR LOWER(name)=?", "area"),
                      ("SELECT DISTINCT well_type FROM wells WHERE LOWER(well_type)=?", "well_type")):
        params = (q, q) if kind == "area" else (q,)
        r = conn.execute(sql, params).fetchone()
        if r:
            col = {"well": "asset_id", "area": "area_id", "well_type": "well_type"}[kind]
            ws = [x[0] for x in conn.execute(f"SELECT asset_id FROM wells WHERE {col}=? ORDER BY asset_id", (r[0],))]
            return {"kind": kind, "id": r[0], "wells": ws}
    # fuzzy area name ("northeast block 7", "block 24")
    for r in conn.execute("SELECT area_id, name FROM areas"):
        if q in (r[1] or "").lower() or (r[1] or "").lower() in q or r[0].lower() in q:
            ws = [x[0] for x in conn.execute("SELECT asset_id FROM wells WHERE area_id=? ORDER BY asset_id", (r[0],))]
            return {"kind": "area", "id": r[0], "wells": ws}
    return {"kind": "unknown", "id": s, "wells": []}


def _in(ids: list[str]) -> str:
    return ",".join("?" * len(ids)) or "NULL"


def downtime(conn, scope: str, start: str, end: str) -> dict:
    """Each inspection in [start, end] that finds the well NOT producing counts as DAYS_PER_INSPECTION days."""
    sc = resolve_scope(conn, scope)
    ids = sc["wells"]
    rows = conn.execute(
        f"""SELECT asset_id, COUNT(*) AS n_down, GROUP_CONCAT(inspection_id) AS evidence
            FROM inspection_effective
            WHERE asset_id IN ({_in(ids)}) AND inspected_date BETWEEN ? AND ? AND producing = 0
            GROUP BY asset_id""", (*ids, start, end)).fetchall()
    per = {r["asset_id"]: r for r in rows}
    out = [{"asset_id": w, "well_name": n, "area_id": a, "well_type": t,
            "downtime_days": (per[w]["n_down"] * config.DAYS_PER_INSPECTION) if w in per else 0,
            "non_producing_inspections": per[w]["n_down"] if w in per else 0,
            "evidence_ids": per[w]["evidence"].split(",") if w in per else []}
           for w, n, a, t in conn.execute(
               f"SELECT asset_id, name, area_id, well_type FROM wells WHERE asset_id IN ({_in(ids)})", ids)]
    out.sort(key=lambda r: (-r["downtime_days"], r["asset_id"]))
    return {"scope": sc["id"], "scope_kind": sc["kind"], "start": start, "end": end,
            "days_per_non_producing_inspection": config.DAYS_PER_INSPECTION,
            "total_days": sum(r["downtime_days"] for r in out), "wells": out}


def spills(conn, scope: str, start: str, end: str, include_corrected: bool = False) -> dict:
    sc = resolve_scope(conn, scope)
    ids = sc["wells"]
    col = "raw_spill_detected" if include_corrected else "spill_detected"
    vol = "raw_spill_est_volume_bbl" if include_corrected else "spill_est_volume_bbl"
    rows = conn.execute(
        f"""SELECT inspection_id, asset_id, area_id, inspected_date, {vol} AS volume_bbl, spill_area_pct,
                   spill_correction_id, volume_correction_id, raw_spill_detected
            FROM inspection_effective
            WHERE asset_id IN ({_in(ids)}) AND inspected_date BETWEEN ? AND ? AND {col} = 1
            ORDER BY inspected_date""", (*ids, start, end)).fetchall()
    items = [dict(r) for r in rows]
    excluded = []
    if not include_corrected:
        excluded = [dict(r) for r in conn.execute(
            f"""SELECT inspection_id, asset_id, inspected_date, raw_spill_est_volume_bbl AS volume_bbl,
                       spill_correction_id AS correction_id
                FROM inspection_effective
                WHERE asset_id IN ({_in(ids)}) AND inspected_date BETWEEN ? AND ?
                  AND raw_spill_detected = 1 AND spill_detected = 0""", (*ids, start, end))]
    return {"scope": sc["id"], "scope_kind": sc["kind"], "start": start, "end": end,
            "include_corrected": include_corrected, "count": len(items),
            "total_volume_bbl": round(sum(i["volume_bbl"] or 0 for i in items), 2),
            "spills": items, "excluded_by_correction": excluded}
