"""Generic dataset loader: walks a dataset folder and loads fleet files and every assets/*/ folder.

Nothing here knows well IDs, areas, counts or dates; it follows whatever is on disk.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import docsearch
from .fleetdocs import FleetDoc, read_text
from .reports import parse_extra, parse_ops_categories, parse_report

DATA_TABLES = ["teams", "areas", "users", "wells", "inspections", "maintenance", "regulatory_events",
               "notes", "report_facts", "doc_chunks", "datasets", "well_extra", "inspection_extra",
               "maintenance_extra", "well_type_history", "well_team_history", "report_extra"]

# Language cue used to turn a dataset "correction" note into a structured override.
# A correction note says what the AI detection really was (rainwater, shadow, ...). Free-text
# corrections that do not match stay as notes and are still shown as context.
NOT_A_SPILL = re.compile(
    r"\bnot\s+(?:a\s+)?(?:crude\s+)?(?:oil\s+)?spill\b|\bno\s+crude\b|\bnot\s+crude\b|\bfalse\s+positive\b"
    r"|\brainwater\b|\bshadow\b|\bproduced\s+water\b|\bwet\s+soil\b", re.I)
DETECTION_STANDS = re.compile(r"detection\s+stands|is\s+crude\s+oil", re.I)


def read_jsonl(path: Path, warn: list[str]) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(read_text(path).splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            warn.append(f"{path}:{n} invalid JSON line skipped")
    return rows


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(read_text(path))


def _b(v):
    return None if v is None else int(bool(v))


def load_dataset(conn, data_dir: Path, name: str, warn: list[str]) -> dict:
    data_dir = Path(data_dir)
    stats = {"wells": 0, "inspections": 0, "maintenance": 0, "regulatory_events": 0, "notes": 0,
             "reports": 0, "reports_missing": 0, "corrections": 0}
    fleet = data_dir / "fleet"

    areas = read_json(fleet / "areas.json", {})
    for t in areas.get("teams", []):
        conn.execute("INSERT OR REPLACE INTO teams VALUES (?,?,?,?)",
                     (t["team_id"], t.get("name"), t.get("lead_user_id"), name))
    for a in areas.get("areas", []):
        c = a.get("center") or {}
        conn.execute("INSERT OR REPLACE INTO areas VALUES (?,?,?,?,?,?)",
                     (a["area_id"], a.get("name"), a.get("team_id"), c.get("lat"), c.get("lon"), name))
    for u in read_json(fleet / "users.json", {}).get("users", []):
        conn.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?)",
                     (u["user_id"], u.get("name"), u.get("role"), u.get("team_id"), name))

    chunks: list[dict] = []
    cats: list = []
    for sp in sorted((fleet / "sops").glob("*.md")) if (fleet / "sops").exists() else []:
        cats = parse_ops_categories(read_text(sp)) or cats
    assets = data_dir / "assets"
    for adir in sorted(p for p in assets.iterdir() if p.is_dir()) if assets.exists() else []:
        meta = read_json(adir / "metadata.json", None)
        if not meta:
            warn.append(f"{adir} has no metadata.json; skipped")
            continue
        wid = meta.get("asset_id", adir.name)
        loc = meta.get("location") or {}
        li = meta.get("last_inspection") or {}
        conn.execute("INSERT OR REPLACE INTO wells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (wid, meta.get("name"), meta.get("well_type"), meta.get("area_id"), meta.get("team_id"),
                      meta.get("in_charge_user_id"), loc.get("lat"), loc.get("lon"), meta.get("installed_on"),
                      meta.get("recorded_status"), li.get("inspection_id"),
                      json.dumps(meta.get("equipment", [])), name))
        stats["wells"] += 1
        conn.execute("INSERT OR REPLACE INTO well_extra VALUES (?,?,?)",
                     (wid, meta.get("status"), meta.get("decommissioned_on")))
        for h in meta.get("type_history") or []:
            conn.execute("INSERT INTO well_type_history VALUES (?,?,?,?)", (wid, h.get("well_type"), h.get("from"), h.get("to")))
        for h in meta.get("team_history") or []:
            conn.execute("INSERT INTO well_team_history VALUES (?,?,?,?)", (wid, h.get("team_id"), h.get("from"), h.get("to")))

        ins = read_jsonl(adir / "inspections.jsonl", warn)
        conn.executemany(
            "INSERT OR REPLACE INTO inspections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(i["inspection_id"], i.get("asset_id", wid), i["inspected_at"], i["inspected_at"][:10],
              i.get("inspector_user_id"), i.get("method"), i.get("ambient_temp_c"), i.get("wellhead_temp_c"),
              _b(i.get("spill_detected")), i.get("spill_area_pct"), i.get("spill_est_volume_bbl"),
              json.dumps(i.get("evidence_images", [])), i.get("notes"), name) for i in ins])
        stats["inspections"] += len(ins)
        conn.executemany("INSERT OR REPLACE INTO inspection_extra VALUES (?,?)",
                         [(i["inspection_id"], i.get("recorded_at")) for i in ins if i.get("recorded_at")])
        raw_spill = {i["inspection_id"]: _b(i.get("spill_detected")) for i in ins}

        for m in read_jsonl(adir / "maintenance.jsonl", warn):
            cost = m.get("cost_usd") or {}
            conn.execute("INSERT OR REPLACE INTO maintenance VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (m["maintenance_id"], m.get("asset_id", wid), m["started_at"], m["started_at"][:10],
                          m.get("completed_at"), m.get("maintenance_type"), m.get("equipment_kind"),
                          m.get("issue"), m.get("action_taken"), m.get("technician_user_id"),
                          m.get("contractor"), m.get("downtime_hours"), cost.get("labor"), cost.get("parts"),
                          cost.get("total"), m.get("triggered_by_inspection_id"), m.get("report_file"), name))
            stats["maintenance"] += 1
            cr = m.get("cost_revision")
            if cr:
                conn.execute("INSERT OR REPLACE INTO maintenance_extra VALUES (?,?,?,?)",
                             (m["maintenance_id"], cr.get("original_total"), cr.get("revised_on"), cr.get("reason")))
            rf = m.get("report_file")
            if rf:
                rpath = adir / rf
                if rpath.exists():
                    text = read_text(rpath)
                    facts = parse_report(text)
                    conn.execute(
                        "INSERT OR REPLACE INTO report_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (m["maintenance_id"], m.get("asset_id", wid), str(rpath), 1, facts["in_progress"],
                         facts["discovery_inspection_id"] or m.get("triggered_by_inspection_id"),
                         facts["spill_class"], facts["spill_volume_bbl"], facts["spill_area_pct"],
                         facts["containment_hours"], facts["reporting_duty_listed"],
                         facts["written_notice_hours"], facts["hse_notice_hours"], facts["root_cause"],
                         facts["action_required"], facts["action_required_text"], facts["compliance_json"],
                         m["maintenance_id"]))
                    chunks += docsearch.chunk_report(m["maintenance_id"], m.get("asset_id", wid), str(rpath), text)
                    ex = parse_extra(text, facts["root_cause"], cats)
                    conn.execute("INSERT OR REPLACE INTO report_extra VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (m["maintenance_id"], m.get("asset_id", wid), ex["pump_model"], ex["pump_serial"],
                                  ex["pump_lot"], ex["followup"], ex["delay_days"], ex["delay_reason"],
                                  ex["soil_sample_sent"], ex["h2s_ppm"], ex["failure_category"]))
                    stats["reports"] += 1
                else:
                    warn.append(f"{m['maintenance_id']}: report_file {rf} not found")
                    conn.execute("INSERT OR REPLACE INTO report_facts (maintenance_id, asset_id, report_file, "
                                 "report_found) VALUES (?,?,?,0)", (m["maintenance_id"], wid, str(rpath)))
                    stats["reports_missing"] += 1

        for r in read_jsonl(adir / "regulatory-events.jsonl", warn):
            conn.execute("INSERT OR REPLACE INTO regulatory_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (r["event_id"], r.get("asset_id", wid), r.get("date"), r.get("agency"),
                          r.get("event_type"), r.get("protocol_id"), r.get("related_inspection_id"),
                          r.get("hours_to_report"), r.get("detail"), r.get("fine_usd"), r.get("status"), name))
            stats["regulatory_events"] += 1

        for n, c in enumerate(read_jsonl(adir / "context.jsonl", warn), 1):
            nid = c.get("note_id") or f"CTX-{wid[-4:]}-auto{n}"
            conn.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (nid, c.get("asset_id", wid), c.get("created_at"), c.get("author_user_id"),
                          c.get("note_type"), c.get("inspection_id"), c.get("text"), c.get("valid_from"),
                          c.get("valid_to"), name))
            stats["notes"] += 1
            if c.get("note_type") == "data_correction" and c.get("inspection_id") and c.get("field"):
                conn.execute("INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"COR-{nid}", c["inspection_id"], c["field"], str(c.get("corrected_value")),
                              str(c.get("original_value")), "dataset", nid, c.get("author_user_id"),
                              c.get("created_at"), c.get("text"), "confirmed", name))
                stats["corrections"] += 1
            if (c.get("note_type") == "correction" and c.get("inspection_id")
                    and NOT_A_SPILL.search(c.get("text", "")) and not DETECTION_STANDS.search(c.get("text", ""))):
                conn.execute("INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"COR-{nid}", c["inspection_id"], "spill_detected", "0",
                              str(raw_spill.get(c["inspection_id"])), "dataset", nid,
                              c.get("author_user_id"), c.get("created_at"), c.get("text"), "confirmed", name))
                stats["corrections"] += 1

    conn.execute("INSERT OR REPLACE INTO datasets VALUES (?,?,?)",
                 (name, str(data_dir), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.executemany("INSERT OR REPLACE INTO doc_chunks VALUES (?,?,?,?,?,?,?)",
                     [(c["chunk_id"], c["doc_id"], c["doc_type"], c["path"], c["asset_id"], c["section"],
                       c["text"]) for c in chunks])
    return stats


def load_fleet_doc_chunks(conn, docs: list[FleetDoc]) -> int:
    chunks = [c for d in docs for c in docsearch.chunk_fleet_doc(d)]
    conn.executemany("INSERT OR REPLACE INTO doc_chunks VALUES (?,?,?,?,?,?,?)",
                     [(c["chunk_id"], c["doc_id"], c["doc_type"], c["path"], c["asset_id"], c["section"],
                       c["text"]) for c in chunks])
    return len(chunks)


def wipe_data(conn) -> None:
    for t in DATA_TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM corrections WHERE source='dataset'")
    conn.commit()
