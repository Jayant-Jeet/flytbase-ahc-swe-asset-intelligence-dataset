"""Rules extraction: manuals and SOPs -> reviewable rules/rules.json -> `rules` table.

Two extractors produce the same JSON shape:
  * deterministic (regex over the documents; default, no API key needed)
  * LLM (`python -m app.rules --llm`), validated against the same schema
Tools read thresholds only from the `rules` table, never from constants in code.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, db
from .fleetdocs import FleetDoc, collect_fleet_docs, docs_hash

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10}


def _n(s: str) -> float:
    s = s.strip().lower()
    return float(NUM_WORDS[s]) if s in NUM_WORDS else float(s)


def _rule(key, scope_type, scope_id, value, unit, source, section, description):
    return {"rule_key": key, "scope_type": scope_type, "scope_id": scope_id,
            "value_num": value if isinstance(value, (int, float)) else None,
            "value_text": value if isinstance(value, str) else None,
            "unit": unit, "source": source, "section": section, "description": description}


def _find(pattern: str, text: str, flags=re.I | re.S):
    return re.search(pattern, text, flags)


def _section_of(text: str, pos: int) -> str:
    heads = [m for m in re.finditer(r"^#{2,4}\s+(.+)$", text[:pos], re.M)]
    if not heads:
        return ""
    num = re.match(r"([\d.]+)", heads[-1].group(1))
    return num.group(1).rstrip(".") if num else heads[-1].group(1)


# ---------------------------------------------------------------- deterministic extractor
WELL_TYPE_PATTERNS = [
    ("producing_min_delta_c", r"at\s+least\s+\*{0,2}(\d+(?:\.\d+)?)\s*°C\s+above[\s*]+ambient", "°C",
     "Producing when wellhead temp minus ambient is at least this"),
    ("temp_alarm_max_c", r"at\s+or\s+below\s+\*{0,2}(\d+(?:\.\d+)?)\s*°C", "°C",
     "Wellhead temperature alarm limit"),
    ("spill_shutin_area_pct", r"more\s+than\s+\*{0,2}(\d+(?:\.\d+)?)\s*%\s+of\s+the\s+pad", "% of pad",
     "Spill area above this requires immediate shut-in"),
    ("long_downtime_days", r"(?:more\s+than|over)\s+(\d+)\s+consecutive\s+days", "days",
     "Downtime longer than this must be reported to the area production supervisor"),
]


def _extract_well_type(doc: FleetDoc) -> tuple[dict[str, dict], str | None]:
    t = doc.text
    out: dict[str, dict] = {}
    for key, pat, unit, desc in WELL_TYPE_PATTERNS:
        m = _find(pat, t)
        if m:
            out[key] = _rule(key, "well_type", doc.key, float(m.group(1)), unit, doc.doc_id,
                             _section_of(t, m.start()), desc)
    m = (_find(r"(\w+)\s+or\s+more\s+\*{0,2}corrective", t) or _find(r"after\s+(\w+)\s+corrective", t))
    if m and (m.group(1).lower() in NUM_WORDS or m.group(1).isdigit()):
        sec = _section_of(t, m.start())
        out["rca_trigger_corrective_count"] = _rule(
            "rca_trigger_corrective_count", "well_type", doc.key, _n(m.group(1)), "repairs",
            doc.doc_id, sec, "Corrective repairs on the same equipment kind that trigger a root-cause analysis")
        w = _find(r"(\d+)\s+months", t[m.start():])
        if w:
            out["rca_window_months"] = _rule("rca_window_months", "well_type", doc.key, float(w.group(1)),
                                             "months", doc.doc_id, sec, "Look-back window for the RCA count")
    base = _find(r"(?:follow\s+the|same\s+as)\s+(T\d+)", t)
    return out, (base.group(1) if base else None)


def _protocol_rows(text: str) -> dict[str, str]:
    rows = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*\*\*([A-Z]{2}-\d+)\*\*\s*\|(.*)$", line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def _extract_sop(doc: FleetDoc) -> list[dict]:
    t, src, out = doc.text, doc.doc_id, []
    rows = _protocol_rows(t)

    # Spill classes table: | Minor | below 1 bbl | ...
    for m in re.finditer(r"^\|\s*(Minor|Moderate|Reportable|Major)\s*\|([^|]*)\|([^|]*)\|", t, re.M | re.I):
        cls, vol, notes = m.group(1).lower(), m.group(2), m.group(3)
        nums = re.findall(r"(\d+(?:\.\d+)?)\s*bbl", vol)
        lo = 0.0 if re.search(r"^\s*below", vol, re.I) else float(nums[0])
        out.append(_rule("spill_class_min_bbl", "global", cls, lo, "bbl", src, "3",
                         f"Lower bound of the '{cls}' spill class"))
        h = re.search(r"notify\s+HSE\s+within\s+(\d+)\s*h", notes, re.I)
        if h:
            out.append(_rule("hse_notice_hours", "global", cls, float(h.group(1)), "hours", src, "3",
                             f"HSE notice deadline for {cls} spills"))
    if (r := rows.get("SR-01")):
        m = _find(r"within\s+\*\*(\d+)\s+hours\*\*", r)
        if m:
            out.append(_rule("sr01_report_hours", "global", "default", float(m.group(1)), "hours", src, "4",
                             "SR-01 written notice deadline after discovery"))
        m = _find(r"or\s+within\s+\*\*(\d+)\s+hours\*\*\s+for\s+a\s+major", r)
        if m:
            out.append(_rule("sr01_report_hours", "global", "major", float(m.group(1)), "hours", src, "4",
                             "SR-01 written notice deadline for a major spill"))
    if (r := rows.get("SR-02")):
        m = _find(r"within\s+\*\*(\d+)\s+hours\*\*", r)
        if m:
            out.append(_rule("sr02_containment_hours", "global", "default", float(m.group(1)), "hours", src,
                             "4", "SR-02 containment deadline after discovery"))
    if (r := rows.get("SR-03")):
        for key, pat, unit, desc in (
            ("sr03_verify_days", r"within\s+\*{0,2}(\d+)\s+days", "days",
             "SR-03 agency verification deadline after discovery (reportable and major spills)"),
            ("sr03_min_soil_samples", r"at\s+least\s+\*{0,2}(\d+)\s+soil\s+samples", "samples",
             "Minimum soil samples for a reportable spill"),
            ("sr03_target_tph_pct", r"\*\*(\d+(?:\.\d+)?)%\s+TPH", "% TPH", "Clean-up target"),
        ):
            m = _find(pat, r)
            if m:
                out.append(_rule(key, "global", "default", float(m.group(1)), unit, src, "4", desc))
    if (r := rows.get("RC-01")):
        m = _find(r"every\s+\*\*(\d+)\s+days\*\*", r)
        if m:
            out.append(_rule("rc01_inspection_interval_days", "global", "default", float(m.group(1)), "days",
                             src, "2", "Maximum days between inspections"))
    if (r := rows.get("RC-02")) and _find(r"once\s+per\s+calendar\s+year", r):
        out.append(_rule("rc02_flowline_tests_per_year", "global", "default", 1.0, "tests/year", src, "2",
                         "Flowline integrity tests required per calendar year"))
    if (r := rows.get("RC-03")):
        m = _find(r"\*\*(\d+)\s+consecutive\s+days\*\*", r)
        if m:
            out.append(_rule("rc03_report_downtime_days", "global", "default", float(m.group(1)), "days", src,
                             "2", "Downtime beyond this is reported to the area production supervisor"))
    if (r := rows.get("RC-04")):
        m = _find(r"within\s+\*\*(\d+)\s+days\*\*", r)
        if m:
            out.append(_rule("rc04_nov_response_days", "global", "default", float(m.group(1)), "days", src, "2",
                             "Notice-of-violation response deadline"))
    return out


def extract_deterministic(docs: list[FleetDoc]) -> list[dict]:
    rules: list[dict] = []
    per_type: dict[str, dict[str, dict]] = {}
    bases: dict[str, str | None] = {}
    for d in docs:
        if d.kind == "manual":
            per_type[d.key], bases[d.key] = _extract_well_type(d)
        else:
            rules += _extract_sop(d)
    for wt, found in per_type.items():  # inherit what a manual delegates to its base manual
        base = bases.get(wt)
        if base and base in per_type:
            for key, r in per_type[base].items():
                if key not in found:
                    found[key] = {**r, "scope_id": wt, "source": f"{r['source']} (inherited by {wt})"}
        rules += list(found.values())
    return rules


# ---------------------------------------------------------------- LLM extractor
LLM_SYSTEM = "You extract operating rules from oil-well manuals and SOPs. Output only JSON."


def extract_with_llm(docs: list[FleetDoc], reference: list[dict]) -> list[dict]:
    from .llm import get_provider

    keys = sorted({r["rule_key"] for r in reference})
    body = "\n\n".join(f"=== {d.doc_id} ({d.kind}, key={d.key}) ===\n{d.text}" for d in docs)
    prompt = (
        "Extract every numeric rule you can find for these rule keys: " + ", ".join(keys) + ".\n"
        "Return JSON {\"rules\": [{\"rule_key\", \"scope_type\" (well_type|global), \"scope_id\" "
        "(well type id, spill class name, 'default' or 'major'), \"value_num\", \"unit\", \"source\" "
        "(document id), \"section\", \"description\"}]}. If a manual defers to another manual, repeat "
        "the rule for the deferring well type. Use only values stated in the text.\n\n" + body)
    raw = get_provider().complete(LLM_SYSTEM, prompt, json_mode=True)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    items = json.loads(raw)["rules"]
    out = []
    for it in items:
        if it.get("rule_key") in keys and isinstance(it.get("value_num"), (int, float)):
            out.append(_rule(it["rule_key"], it.get("scope_type", "global"), str(it.get("scope_id", "default")),
                             float(it["value_num"]), it.get("unit"), it.get("source"), it.get("section"),
                             it.get("description")))
    return out


# ---------------------------------------------------------------- persistence
def build_rules_file(docs: list[FleetDoc], use_llm: bool = False) -> dict:
    rules = extract_deterministic(docs)
    by = "deterministic-regex"
    if use_llm:
        llm_rules = extract_with_llm(docs, rules)
        det = {(r["rule_key"], r["scope_type"], r["scope_id"]): r["value_num"] for r in rules}
        llm = {(r["rule_key"], r["scope_type"], r["scope_id"]): r["value_num"] for r in llm_rules}
        for k in sorted(set(det) | set(llm)):
            if det.get(k) != llm.get(k):
                print(f"  DIFF {k}: regex={det.get(k)} llm={llm.get(k)}", file=sys.stderr)
        rules, by = llm_rules or rules, "llm"
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "extracted_by": by, "docs_hash": docs_hash(docs), "reviewed": False, "rules": rules}


def write_rules_file(data: dict, path: Path | None = None) -> Path:
    path = Path(path or config.rules_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    data["rules"] = sorted(data["rules"], key=lambda r: (r["rule_key"], r["scope_type"], r["scope_id"]))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def ensure_rules_file(docs: list[FleetDoc], force: bool = False, use_llm: bool = False) -> dict:
    """Load rules.json; regenerate when missing, forced, or when the source documents changed."""
    path = config.rules_path()
    if path.exists() and not force:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("docs_hash") == docs_hash(docs):
            return data
        print(f"! Source documents changed since {path.name} was generated; re-extracting. "
              "Review the new file.", file=sys.stderr)
    data = build_rules_file(docs, use_llm)
    write_rules_file(data, path)
    print(f"Wrote {path} ({len(data['rules'])} rules, by {data['extracted_by']}). Please review it.")
    return data


def load_rules_table(conn, data: dict) -> int:
    conn.execute("DELETE FROM rules")
    conn.executemany(
        "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?,?,?)",
        [(r["rule_key"], r["scope_type"], r["scope_id"], r.get("value_num"), r.get("value_text"),
          r.get("unit"), r.get("source"), r.get("section"), r.get("description")) for r in data["rules"]])
    conn.commit()
    return len(data["rules"])


def get_rule(conn, key: str, well_type: str | None = None, scope_id: str = "default") -> float | None:
    """Rule lookup: well-type scope first (when given), then global scope."""
    if well_type:
        row = conn.execute("SELECT value_num FROM rules WHERE rule_key=? AND scope_type='well_type' "
                           "AND scope_id=?", (key, well_type)).fetchone()
        if row:
            return row["value_num"]
    row = conn.execute("SELECT value_num FROM rules WHERE rule_key=? AND scope_type='global' AND scope_id=?",
                       (key, scope_id)).fetchone()
    return row["value_num"] if row else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract rules from manuals/SOPs into rules/rules.json")
    ap.add_argument("--data-dir", action="append", help="dataset folder (repeatable)")
    ap.add_argument("--llm", action="store_true", help="use the configured LLM instead of regex")
    a = ap.parse_args()
    dirs = [Path(p) for p in a.data_dir] if a.data_dir else config.data_dirs()
    ensure_rules_file(collect_fleet_docs(dirs), force=True, use_llm=a.llm)


if __name__ == "__main__":
    main()
