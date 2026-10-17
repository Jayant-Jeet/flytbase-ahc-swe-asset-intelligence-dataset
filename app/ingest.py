"""python -m app.ingest --data-dir <path> [--data-dir <path2>] [--reset] [--llm-rules]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, db, docsearch, loader, rules
from .fleetdocs import collect_fleet_docs


def ingest(data_dirs: list[Path], reset: bool = True, use_llm_rules: bool = False, db_file: Path | None = None) -> dict:
    conn = db.connect(db_file)
    if reset:
        db.reset_data(conn)
    else:
        db.init_schema(conn)
        loader.wipe_data(conn)
    docs = collect_fleet_docs(data_dirs)
    rules_data = rules.ensure_rules_file(docs, use_llm=use_llm_rules)
    n_rules = rules.load_rules_table(conn, rules_data)
    report: dict = {"rules": n_rules, "datasets": {}, "warnings": []}
    for d in data_dirs:
        report["datasets"][d.name] = loader.load_dataset(conn, d, d.name, report["warnings"])
    report["fleet_chunks"] = loader.load_fleet_doc_chunks(conn, docs)
    conn.commit()
    report["doc_chunks"] = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
    conn.close()
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Load dataset folder(s) into the local database.")
    ap.add_argument("--data-dir", action="append", help="dataset folder (repeatable); default: DATA_DIR env")
    ap.add_argument("--reset", action="store_true", help="drop all tables first (default: replace data in place, "
                                                         "keep user facts)")
    ap.add_argument("--llm-rules", action="store_true", help="also ask the LLM to extract rules and show diffs")
    a = ap.parse_args()
    dirs = [Path(p) for p in a.data_dir] if a.data_dir else config.data_dirs()
    for d in dirs:
        if not (d / "assets").exists() and not (d / "fleet").exists():
            sys.exit(f"{d} does not look like a dataset folder (no assets/ or fleet/)")
    r = ingest(dirs, reset=a.reset, use_llm_rules=a.llm_rules)
    print(f"rules loaded: {r['rules']}  (review {config.rules_path()})")
    for name, s in r["datasets"].items():
        print(f"dataset {name}: " + ", ".join(f"{k}={v}" for k, v in s.items()))
    print(f"doc chunks: {r['doc_chunks']}")
    for w in r["warnings"][:20]:
        print("warning:", w)
    if len(r["warnings"]) > 20:
        print(f"... {len(r['warnings']) - 20} more warnings")


if __name__ == "__main__":
    main()
