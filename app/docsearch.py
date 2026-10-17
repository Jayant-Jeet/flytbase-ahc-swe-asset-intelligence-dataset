"""Chunking of manuals, SOPs and repair reports, plus an in-memory BM25 index with citable chunk IDs."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from .fleetdocs import FleetDoc
from .reports import split_sections

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    toks = []
    for t in _TOKEN.findall(text.lower()):
        toks.append(t)
        if "-" in t:  # ids like mnt-0001-029 also match by parts
            toks.extend(t.split("-"))
    return [_stem(t) for t in toks]


def _stem(t: str) -> str:
    for suf in ("ments", "ment", "ings", "ing", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def chunk_fleet_doc(doc: FleetDoc) -> list[dict]:
    chunks = []
    title = re.search(r"^#\s+(.+)$", doc.text, re.M)
    title = title.group(1) if title else doc.doc_id
    parts = re.split(r"^(#{2,3})\s+(.+)$", doc.text, flags=re.M)
    if len(parts) == 1 or parts[0].strip().count("\n") > 3:
        pre = re.sub(r"^#\s+.+$", "", parts[0], count=1, flags=re.M).strip()
        chunks.append({"chunk_id": f"{doc.doc_id}#intro", "doc_id": doc.doc_id, "doc_type": doc.kind,
                       "path": str(doc.path), "asset_id": None, "section": "intro",
                       "text": f"{title}\n{pre}"})
    for i in range(1, len(parts), 3):
        heading, body = parts[i + 1].strip(), parts[i + 2].strip()
        num = re.match(r"([\d.]+)", heading)
        sec = num.group(1).rstrip(".") if num else slug(heading)
        chunks.append({"chunk_id": f"{doc.doc_id}#{sec}", "doc_id": doc.doc_id, "doc_type": doc.kind,
                       "path": str(doc.path), "asset_id": None, "section": heading,
                       "text": f"{title} - {heading}\n{body}"})
        for line in body.splitlines():  # one chunk per SOP protocol row (SR-01, RC-04, WS-02, ...)
            m = re.match(r"^\|\s*\*\*([A-Z]{2}-\d+)\*\*\s*\|(.*)$", line)
            if m:
                chunks.append({"chunk_id": f"{doc.doc_id}#{m.group(1)}", "doc_id": doc.doc_id,
                               "doc_type": doc.kind, "path": str(doc.path), "asset_id": None,
                               "section": m.group(1), "text": f"{title} - {m.group(1)}\n{m.group(2)}"})
    return chunks


def chunk_report(maintenance_id: str, asset_id: str, path: str, text: str) -> list[dict]:
    out, seen = [], Counter()
    for heading, body in split_sections(text):
        if not body:
            continue
        s = slug(heading)
        seen[s] += 1
        cid = f"{maintenance_id}#{s}" + (f"-{seen[s]}" if seen[s] > 1 else "")
        out.append({"chunk_id": cid, "doc_id": maintenance_id, "doc_type": "report", "path": path,
                    "asset_id": asset_id, "section": heading,
                    "text": f"Repair report {maintenance_id} ({asset_id}) - {heading}\n{body}"})
    return out


class DocIndex:
    def __init__(self, chunks: list[dict], k1: float = 1.5, b: float = 0.75):
        self.chunks, self.k1, self.b = chunks, k1, b
        self.tf: list[Counter] = [Counter(tokenize(c["text"])) for c in chunks]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        self.inv: dict[str, list[int]] = defaultdict(list)
        for i, t in enumerate(self.tf):
            for term in t:
                self.inv[term].append(i)

    @classmethod
    def from_db(cls, conn) -> "DocIndex":
        return cls([dict(r) for r in conn.execute("SELECT * FROM doc_chunks")])

    def search(self, query: str, k: int = 5, doc_type: str | None = None,
               asset_id: str | None = None) -> list[dict]:
        n = len(self.chunks)
        scores: dict[int, float] = defaultdict(float)
        for term in set(tokenize(query)):
            postings = self.inv.get(term)
            if not postings:
                continue
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for i in postings:
                c = self.chunks[i]
                if (doc_type and c["doc_type"] != doc_type) or (asset_id and c["asset_id"] != asset_id):
                    continue
                f = self.tf[i][term]
                scores[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
        return [{**{x: self.chunks[i][x] for x in ("chunk_id", "doc_id", "doc_type", "section", "asset_id")},
                 "score": round(s, 3), "text": self.chunks[i]["text"][:1200]} for i, s in top]
