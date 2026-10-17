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
from .reports import parse_report

DATA_TABLES = ["teams", "areas", "users", "wells", "inspections", "maintenance", "regulatory_events",
               "notes", "report_facts", "doc_chunks", "datasets"]

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

        ins = read_jsonl(adir / "inspections.jsonl", warn)
        conn.executemany(
            "INSERT OR REPLACE INTO inspections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(i["inspection_id"], i.get("asset_id", wid), i["inspected_at"], i["inspected_at"][:10],
              i.get("inspector_user_id"), i.get("method"), i.get("ambient_temp_c"), i.get("wellhead_temp_c"),
              _b(i.get("spill_detected")), i.get("spill_area_pct"), i.get("spill_est_volume_bbl"),
              json.dumps(i.get("evidence_images", [])), i.get("notes"), name) for i in ins])
        stats["inspections"] += len(ins)
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
