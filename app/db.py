"""SQLite schema, connection helper and derived views."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

TABLES = """
CREATE TABLE IF NOT EXISTS datasets (dataset TEXT PRIMARY KEY, path TEXT, loaded_at TEXT);
CREATE TABLE IF NOT EXISTS teams (team_id TEXT PRIMARY KEY, name TEXT, lead_user_id TEXT, dataset TEXT);
CREATE TABLE IF NOT EXISTS areas (
  area_id TEXT PRIMARY KEY, name TEXT, team_id TEXT, center_lat REAL, center_lon REAL, dataset TEXT);
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY, name TEXT, role TEXT, team_id TEXT, dataset TEXT);
CREATE TABLE IF NOT EXISTS wells (
  asset_id TEXT PRIMARY KEY, name TEXT, well_type TEXT, area_id TEXT, team_id TEXT,
  in_charge_user_id TEXT, lat REAL, lon REAL, installed_on TEXT, recorded_status TEXT,
  last_inspection_id TEXT, equipment_json TEXT, dataset TEXT);
CREATE TABLE IF NOT EXISTS inspections (
  inspection_id TEXT PRIMARY KEY, asset_id TEXT, inspected_at TEXT, inspected_date TEXT,
  inspector_user_id TEXT, method TEXT, ambient_temp_c REAL, wellhead_temp_c REAL,
  spill_detected INTEGER, spill_area_pct REAL, spill_est_volume_bbl REAL,
  evidence_images TEXT, notes TEXT, dataset TEXT);
CREATE INDEX IF NOT EXISTS ix_ins_asset_date ON inspections(asset_id, inspected_at);
CREATE TABLE IF NOT EXISTS maintenance (
  maintenance_id TEXT PRIMARY KEY, asset_id TEXT, started_at TEXT, started_date TEXT,
  completed_at TEXT, maintenance_type TEXT, equipment_kind TEXT, issue TEXT, action_taken TEXT,
  technician_user_id TEXT, contractor TEXT, downtime_hours REAL, cost_labor REAL,
  cost_parts REAL, cost_total REAL, triggered_by_inspection_id TEXT, report_file TEXT, dataset TEXT);
CREATE INDEX IF NOT EXISTS ix_mnt_asset ON maintenance(asset_id, started_at);
CREATE TABLE IF NOT EXISTS regulatory_events (
  event_id TEXT PRIMARY KEY, asset_id TEXT, date TEXT, agency TEXT, event_type TEXT,
  protocol_id TEXT, related_inspection_id TEXT, hours_to_report REAL, detail TEXT,
  fine_usd REAL, status TEXT, dataset TEXT);
CREATE TABLE IF NOT EXISTS notes (
  note_id TEXT PRIMARY KEY, asset_id TEXT, created_at TEXT, author_user_id TEXT, note_type TEXT,
  inspection_id TEXT, text TEXT, valid_from TEXT, valid_to TEXT, dataset TEXT);
CREATE INDEX IF NOT EXISTS ix_notes_asset ON notes(asset_id);
CREATE TABLE IF NOT EXISTS rules (
  rule_key TEXT NOT NULL, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
  value_num REAL, value_text TEXT, unit TEXT, source TEXT, section TEXT, description TEXT,
  PRIMARY KEY (rule_key, scope_type, scope_id));
CREATE TABLE IF NOT EXISTS report_facts (
  maintenance_id TEXT PRIMARY KEY, asset_id TEXT, report_file TEXT, report_found INTEGER,
  in_progress INTEGER, discovery_inspection_id TEXT, spill_class TEXT, spill_volume_bbl REAL,
  spill_area_pct REAL, containment_hours REAL, reporting_duty_listed INTEGER,
  written_notice_hours REAL, hse_notice_hours REAL, root_cause TEXT,
  action_required INTEGER, action_required_text TEXT, compliance_json TEXT, chunk_prefix TEXT);
-- Overlay of corrections on raw inspection values. Raw rows are never modified.
CREATE TABLE IF NOT EXISTS corrections (
  correction_id TEXT PRIMARY KEY, inspection_id TEXT NOT NULL, field TEXT NOT NULL,
  new_value TEXT, original_value TEXT, source TEXT, source_ref TEXT, author_user_id TEXT,
  created_at TEXT, reason TEXT, status TEXT DEFAULT 'confirmed', dataset TEXT);
CREATE INDEX IF NOT EXISTS ix_corr_ins ON corrections(inspection_id, field);
-- Append-only shared knowledge. Status changes are new rows pointing at the old one via supersedes_id.
CREATE TABLE IF NOT EXISTS facts (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
  text TEXT, payload TEXT, author_user_id TEXT, created_at TEXT, status TEXT NOT NULL,
  supersedes_id TEXT, reason TEXT, original_value TEXT);
CREATE INDEX IF NOT EXISTS ix_facts_scope ON facts(scope_type, scope_id);
-- Private memory: conversations and messages are always filtered by user_id.
CREATE TABLE IF NOT EXISTS conversations (
  conv_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, day TEXT, started_at TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS messages (
  msg_id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, user_id TEXT NOT NULL, day TEXT, ts TEXT,
  role TEXT, content TEXT, tool_name TEXT, tool_args TEXT, wells_json TEXT);
CREATE INDEX IF NOT EXISTS ix_msg_user ON messages(user_id, day);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS user_prefs (user_id TEXT, pref_key TEXT, pref_value TEXT, created_at TEXT,
  PRIMARY KEY (user_id, pref_key));
CREATE TABLE IF NOT EXISTS well_extra (asset_id TEXT PRIMARY KEY, status TEXT, decommissioned_on TEXT);
CREATE TABLE IF NOT EXISTS inspection_extra (inspection_id TEXT PRIMARY KEY, recorded_at TEXT);
CREATE TABLE IF NOT EXISTS maintenance_extra (
  maintenance_id TEXT PRIMARY KEY, cost_original REAL, cost_revised_on TEXT, cost_revision_reason TEXT);
CREATE TABLE IF NOT EXISTS well_type_history (asset_id TEXT, well_type TEXT, from_date TEXT, to_date TEXT);
CREATE INDEX IF NOT EXISTS ix_wth ON well_type_history(asset_id);
CREATE TABLE IF NOT EXISTS well_team_history (asset_id TEXT, team_id TEXT, from_date TEXT, to_date TEXT);
CREATE INDEX IF NOT EXISTS ix_wtmh ON well_team_history(asset_id);
CREATE TABLE IF NOT EXISTS report_extra (
  maintenance_id TEXT PRIMARY KEY, asset_id TEXT, pump_model TEXT, pump_serial TEXT, pump_lot TEXT,
  followup TEXT, delay_days REAL, delay_reason TEXT, soil_sample_sent INTEGER, h2s_ppm REAL,
  failure_category TEXT);
CREATE TABLE IF NOT EXISTS doc_chunks (
  chunk_id TEXT PRIMARY KEY, doc_id TEXT, doc_type TEXT, path TEXT, asset_id TEXT,
  section TEXT, text TEXT);
"""

VIEWS = """
DROP VIEW IF EXISTS inspection_effective;
DROP VIEW IF EXISTS corrections_active;
DROP VIEW IF EXISTS rules_effective;
DROP VIEW IF EXISTS facts_current;
CREATE VIEW facts_current AS
SELECT f.* FROM facts f WHERE NOT EXISTS (SELECT 1 FROM facts g WHERE g.supersedes_id = f.id);

-- Manual/SOP rules, with confirmed user rules for a well type taking precedence (latest wins).
CREATE VIEW rules_effective AS
SELECT r.rule_key, r.scope_type, r.scope_id, r.value_num, r.value_text, r.unit, r.source, r.section, r.description
FROM rules r
WHERE NOT (r.scope_type = 'well_type' AND EXISTS (
  SELECT 1 FROM facts_current f WHERE f.scope_type = 'well_type' AND f.scope_id = r.scope_id
    AND f.status = 'confirmed' AND json_extract(f.payload, '$.rule_key') = r.rule_key))
UNION ALL
SELECT json_extract(f.payload, '$.rule_key'), 'well_type', f.scope_id, json_extract(f.payload, '$.value'), NULL,
       json_extract(f.payload, '$.unit'), 'user:' || f.author_user_id, f.id, f.text
FROM facts_current f
WHERE f.scope_type = 'well_type' AND f.status = 'confirmed' AND json_extract(f.payload, '$.rule_key') IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM facts_current g WHERE g.scope_type = 'well_type' AND g.scope_id = f.scope_id
    AND g.status = 'confirmed' AND json_extract(g.payload, '$.rule_key') = json_extract(f.payload, '$.rule_key')
    AND g.seq > f.seq);

CREATE VIEW corrections_active AS
SELECT c.* FROM corrections c
WHERE c.status IN ('confirmed', 'applied', 'unconfirmed_applied')
  AND c.rowid = (SELECT c2.rowid FROM corrections c2
                 WHERE c2.inspection_id = c.inspection_id AND c2.field = c.field
                   AND c2.status IN ('confirmed', 'applied', 'unconfirmed_applied')
                 ORDER BY (c2.source = 'user') DESC, c2.created_at DESC, c2.rowid DESC LIMIT 1);

CREATE VIEW inspection_effective AS
SELECT
  i.inspection_id, i.asset_id, i.inspected_at, i.inspected_date, i.inspector_user_id, i.method,
  COALESCE(h.well_type, w.well_type) AS well_type, w.area_id, w.name AS well_name,
  i.ambient_temp_c AS raw_ambient_temp_c, i.wellhead_temp_c AS raw_wellhead_temp_c,
  COALESCE(CAST(ca.new_value AS REAL), i.ambient_temp_c) AS ambient_temp_c,
  COALESCE(CAST(cw.new_value AS REAL), i.wellhead_temp_c) AS wellhead_temp_c,
  i.spill_detected AS raw_spill_detected,
  COALESCE(CAST(cs.new_value AS INTEGER), i.spill_detected) AS spill_detected,
  i.spill_est_volume_bbl AS raw_spill_est_volume_bbl,
  COALESCE(CAST(cv.new_value AS REAL), i.spill_est_volume_bbl) AS spill_est_volume_bbl,
  i.spill_area_pct, i.notes,
  r.value_num AS producing_min_delta_c,
  ROUND(COALESCE(CAST(cw.new_value AS REAL), i.wellhead_temp_c)
        - COALESCE(CAST(ca.new_value AS REAL), i.ambient_temp_c), 2) AS delta_c,
  CASE WHEN r.value_num IS NULL OR i.wellhead_temp_c IS NULL OR i.ambient_temp_c IS NULL THEN NULL
       WHEN ROUND(COALESCE(CAST(cw.new_value AS REAL), i.wellhead_temp_c)
                  - COALESCE(CAST(ca.new_value AS REAL), i.ambient_temp_c), 2) >= r.value_num
       THEN 1 ELSE 0 END AS producing,
  cw.correction_id AS wellhead_correction_id, ca.correction_id AS ambient_correction_id,
  cs.correction_id AS spill_correction_id, cv.correction_id AS volume_correction_id,
  (cw.correction_id IS NOT NULL OR ca.correction_id IS NOT NULL
   OR cs.correction_id IS NOT NULL OR cv.correction_id IS NOT NULL) AS corrected
FROM inspections i
JOIN wells w ON w.asset_id = i.asset_id
LEFT JOIN well_type_history h ON h.asset_id = i.asset_id AND h.from_date <= i.inspected_date
                 AND (h.to_date IS NULL OR h.to_date >= i.inspected_date)
LEFT JOIN rules_effective r ON r.rule_key = 'producing_min_delta_c' AND r.scope_type = 'well_type'
                 AND r.scope_id = COALESCE(h.well_type, w.well_type)
LEFT JOIN corrections_active cw ON cw.inspection_id = i.inspection_id AND cw.field = 'wellhead_temp_c'
LEFT JOIN corrections_active ca ON ca.inspection_id = i.inspection_id AND ca.field = 'ambient_temp_c'
LEFT JOIN corrections_active cs ON cs.inspection_id = i.inspection_id AND cs.field = 'spill_detected'
LEFT JOIN corrections_active cv ON cv.inspection_id = i.inspection_id AND cv.field = 'spill_est_volume_bbl';
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(TABLES)
    conn.executescript(VIEWS)
    conn.commit()


def reset_data(conn: sqlite3.Connection) -> None:
    """Drop every table for a clean re-ingest."""
    conn.executescript(TABLES)
    conn.executescript(
        "DROP VIEW IF EXISTS inspection_effective; DROP VIEW IF EXISTS corrections_active;"
        "DROP VIEW IF EXISTS rules_effective; DROP VIEW IF EXISTS facts_current;"
    )
    # facts, user corrections, conversations and messages are memory, not data: they survive a re-ingest
    conn.execute("DELETE FROM corrections WHERE source='dataset'")
    for t in ("datasets", "teams", "areas", "users", "wells", "inspections", "maintenance",
              "regulatory_events", "notes", "rules", "report_facts", "doc_chunks", "well_extra", "inspection_extra",
              "maintenance_extra", "well_type_history", "well_team_history", "report_extra"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    init_schema(conn)
