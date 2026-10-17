"""Locate and read the fleet-wide documents (well-type manuals and SOPs)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FleetDoc:
    doc_id: str      # e.g. OM-T200, SOP-SR
    kind: str        # manual | sop
    key: str         # well type id for manuals (T200), file stem for SOPs
    path: Path
    text: str


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def collect_fleet_docs(data_dirs: list[Path]) -> list[FleetDoc]:
    docs: dict[tuple[str, str], FleetDoc] = {}  # later datasets override earlier ones
    for d in data_dirs:
        for sub, kind in (("well-types", "manual"), ("sops", "sop")):
            for p in sorted((d / "fleet" / sub).glob("*.md")):
                text = read_text(p)
                m = re.search(r"Document ID:\s*([A-Za-z0-9\-]+)", text)
                doc_id = m.group(1) if m else p.stem
                docs[(kind, p.stem)] = FleetDoc(doc_id, kind, p.stem, p, text)
    return list(docs.values())


def docs_hash(docs: list[FleetDoc]) -> str:
    h = hashlib.sha256()
    for d in sorted(docs, key=lambda x: (x.kind, x.key)):
        h.update(d.key.encode())
        h.update(d.text.encode("utf-8"))
    return h.hexdigest()[:16]
