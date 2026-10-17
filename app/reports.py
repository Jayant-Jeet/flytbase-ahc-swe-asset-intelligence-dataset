"""Parse free-text repair reports into structured facts."""
from __future__ import annotations

import json
import re


def _f(m: re.Match | None, group: int = 1) -> float | None:
    return float(m.group(group)) if m else None


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body). Text before the first '## ' is the 'Header' section."""
    parts = re.split(r"^##\s+(.+)$", text, flags=re.M)
    out = [("Header", parts[0].strip())]
    for i in range(1, len(parts), 2):
        out.append((parts[i].strip(), parts[i + 1].strip()))
    return out


def parse_compliance_table(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and re.match(r"^(WS|SR|RC)-\d+$|^HSE notice$|^Manual", cells[0]):
            rows.append({"protocol": cells[0], "result": cells[1].replace("**", ""),
                         "detail": " | ".join(cells[2:])})
    return rows


def parse_report(text: str) -> dict:
    sections = dict(split_sections(text))
    rows = parse_compliance_table(sections.get("Compliance check", ""))
    by_proto = {r["protocol"]: r for r in rows}

    containment = _f(re.search(r"contained\s+(?:about\s+)?\**([\d.]+)\s+hours", text, re.I))
    if containment is None and "SR-02" in by_proto:
        containment = _f(re.search(r"([\d.]+)\s+hours", by_proto["SR-02"]["detail"]))

    sr01 = by_proto.get("SR-01")
    written = _f(re.search(r"([\d.]+)\s+hours", sr01["detail"])) if sr01 else None
    hse = by_proto.get("HSE notice")
    hse_hours = _f(re.search(r"([\d.]+)\s+hours", hse["detail"])) if hse else None
    action = next((r for r in rows if r["result"].lower().startswith("action required")), None)

    sr04 = by_proto.get("SR-04", {}).get("detail", "")
    cls = re.search(r"\((\w+)\s+class\)", sr04)
    trig = re.search(r"\*\*Triggered by:\*\*\s*inspection\s+(INS-[\w-]+)", text)
    discovery = re.search(r"inspection\s+(INS-\d+-\d+)\s+at\s", sections.get("What happened", ""))
    root = sections.get("Root cause", "").strip()

    return {
        "in_progress": int("Work done so far" in sections),
        "discovery_inspection_id": (trig or discovery).group(1) if (trig or discovery) else None,
        "spill_class": cls.group(1).lower() if cls else None,
        "spill_volume_bbl": _f(re.search(r"estimated\s+([\d.]+)\s+bbl", text, re.I)),
        "spill_area_pct": _f(re.search(r"([\d.]+)%\s+of\s+the\s+pad", text, re.I)),
        "containment_hours": containment,
        "reporting_duty_listed": int(sr01 is not None),
        "written_notice_hours": written,
        "hse_notice_hours": hse_hours,
        "root_cause": root,
        "action_required": int(action is not None),
        "action_required_text": action["detail"] if action else None,
        "compliance_json": json.dumps(rows),
    }
