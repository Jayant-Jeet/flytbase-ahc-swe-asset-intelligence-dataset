# System design (one page)

**Principle:** the LLM does language and tool selection; code does all facts and math. Every number in an answer comes from a tool result that also returns the record IDs used as evidence.

## Storage and indexing
- A generic loader walks `DATA_DIR` (one or more folders): `fleet/*.json` plus every `assets/*/` (metadata, inspections, maintenance, regulatory events, context notes). Nothing is hardcoded; re-running on a new folder rebuilds the SQLite DB.
- Views: `inspection_effective` = raw inspection + corrections overlay (raw values never overwritten); producing = (wellhead temp − ambient) ≥ threshold for the well's type.
- `rules` table: extracted once from manuals and SOPs by the LLM (heuristic fallback), saved to `rules/rules.json` for human review. Tools read thresholds only from this table.
- `report_facts`: fields parsed from each repair report (always following `maintenance.report_file`): containment hours, spill class, root cause, reporting duty, action required.
- BM25 index over manuals, SOPs and reports, chunked with citable chunk IDs.

## Tools
`well_status`, `downtime`, `spills`, `list_wells_not_producing`, `compliance_check`, `repair_costs`, `inspection_history`, `root_cause_triggers`, `explain_spill`, `search_docs`, `get_rules`, read-only `sql_query`, `remember_fact`, `confirm_fact`, `record_correction`, `recall`, `render_visual` (schema-validated: status_card, table, line_chart, bar_chart, map, heatmap).
Downtime = 7 days per inspection that finds the well not producing (missed inspections count 0; `downtime_hours` ignored; repairs belong to a period by start date). Inspections, not `recorded_status`, are the truth.

## Numbers, documents and notes together
Numbers come from SQL/derived views; documents (SOP clauses, report text) from `search_docs` with chunk citations; notes (CTX) are surfaced next to the numbers they affect (e.g. planned shut-in beside a not-producing status). "No recorded reason" is stated when nothing explains a shutdown; causes are never invented. The answer ends with an Evidence list (INS-, MNT-, CTX-, REG-, FACT-, COR-, doc chunks).

## Memory
- **Shared:** append-only `facts` (scope well / well_type / global, author, time, status confirmed | unconfirmed | disputed, supersedes, reason, original value). Visible to all users, attributed to the author.
- **Private:** conversations and messages keyed by `user_id`; "the wells we discussed yesterday" resolves only from that user's own history, otherwise the answer is "no such earlier conversation". `user_id` is injected by code, never by the model.
- **Corrections** to raw data or AI analysis are stored against the inspection ID with author and reason, original kept, and re-run downstream logic (producing status, reportability, SOP deadlines) for every later answer.
- **Well-type rules** (e.g. T350 RCA after 2 repairs) apply only to that type and answers say they follow the user's rule, not the manual.

## Conflict handling
Each user claim is compared with derived data and existing facts. On disagreement it is saved as `unconfirmed`, the answer shows what the data says with both sources and asks for confirmation. Corrections that contradict inspector notes in the same inspection are flagged too.

## New data
Same format, more wells/history: re-ingest with `python -m app.ingest --data-dir <path>`. New areas, types, users and shorter histories flow from the data; thresholds come from the extracted rules.

## Models and cost
Gemma 4 (`gemma-4-31b-it`) via Google AI Studio function calling; switchable to OpenRouter (`google/gemma-4-31b-it:free`) or Ollama via env vars. LLM use: one-off rules extraction plus a few tool-calling turns per question; free tier is sufficient for a demo.

## Status and next steps
Tool layer is verified by 38 unit tests and an 18-question eval (all pass deterministically). End-to-end behaviour with the real model (`--llm`) must be run once a key is available; known risks: system-instruction support and tool-schema strictness. Next: LLM-based report parsing fallback, richer anomaly detection, auth, a vector index for free-text notes, per-turn latency caching.
