# Asset Intelligence Copilot — Final Report

## 1. System design (page 1)

**Principle:** the LLM handles language and tool selection only. Code does every fact, count and date calculation. Every number in an answer comes from a tool result that carries record IDs (INS-, MNT-, CTX-, REG-, doc chunk IDs), and the UI shows them in an *Evidence* section.

**Architecture**

```
Browser (HTML/JS, Chart.js, Leaflet, user picker)
   │  SSE stream: tool calls, text, visuals, evidence
FastAPI  ──►  Agent loop (app/agent.py)  ──►  LLM provider interface
                    │                          (Gemma 4 31B via Ollama; Google AI Studio / OpenRouter by env var)
                    ▼
          Tool registry (app/tools.py, app/ops.py)
                    ▼
SQLite: wells, inspections, maintenance, regulatory events, notes, report_facts,
        rules, facts (append-only), user_prefs, conversations, BM25 chunk index
```

**Storage and indexing.** A generic loader (`python -m app.ingest --data-dir <path>`) walks `fleet/*.json` and every `assets/*/` folder. No well IDs, areas or counts are hardcoded, and more than one dataset folder can be loaded. Repair reports are parsed through each record's `report_file` into `report_facts` (containment hours, spill class, root cause, reporting duty). Manuals, SOPs, MOC memos, the regulator letter and reports are chunked into a BM25 index with citable chunk IDs. The LLM extracts the rules once from the SOPs into reviewable JSON (`rules/*.json`), and tools read thresholds from that table.

**How numbers, documents and notes combine.** `inspection_effective` is the raw inspection plus a corrections overlay. Raw values are never overwritten. Producing = wellhead − ambient ≥ the threshold for the well type. Downtime is 7 days per non-producing inspection, and a repair belongs to the period of its start date. Notes provide reasons and operator claims. Documents provide the rules. Tools join all three and return structured JSON with the evidence IDs.

**Tools (about 30).**
- Core: `well_status`, `downtime`, `spills`, `list_wells_not_producing`, `compliance_check`, `repair_costs`, `search_docs`, `get_rules`, read-only `sql_query`.
- Memory: `remember_fact`, `recall`, `record_correction`, `confirm_fact`, `set_preference`.
- Visual: `render_visual` (schema-validated: status_card, table, line, bar incl. stacked, map, heatmap).
- Final-dataset operations:
  - `gas_odor_responses` (OPS-1)
  - `temporary_bypasses` (OPS-2)
  - `status_conflicts` (OPS-3)
  - `freeze_protection_check` (OPS-5)
  - `regulator_letter_spills`
  - `recalled_pumps`
  - `team_info`
  - `repair_delay_reasons`
  - `area_temperature_comparison`
  - `operator_complaints`
  - `data_inventory`

**Memory.** *Shared:* the append-only `facts` table (scope well, well type or global; author, time, status confirmed, unconfirmed or disputed; supersedes, reason, original value). Facts are shown to every user with attribution. *Private:* conversations and `user_prefs` are keyed by user_id, so "yesterday's wells" resolves only from that user's own history and never leaks. *Corrections* are stored against the inspection ID, the original is kept, and they are applied to all later answers, with downstream logic re-run. Rules scoped to a well type apply only to that type and are labelled as the user's rule.

**Conflict and honesty handling.** A user claim is compared with derived data and existing facts. On disagreement it is stored as *unconfirmed*, and the answer shows both sources and asks for confirmation. Second-hand claims are recorded with who the claimant heard it from. When the data cannot answer a question, `data_inventory` lists what exists and the answer says so, offers proxies and never invents a number. Ambiguous questions state both readings and the assumption.

**New data.** Thresholds come from the rules table and dates from a configurable `TODAY`. Dataset paths, rules file and DB path are env vars, so the hidden set needs no code change. Re-ingest is one command.

**Models and cost.** `gemma-4-31b-it` locally via Ollama (temperature 0, 16k context). There is no API cost, but it takes minutes per question. The provider interface switches to AI Studio or OpenRouter by env var.

**Testing.** 38 pytest tests; `python -m tests.eval_questions` passes 18/18 on the sample set; `python -m tests.eval_questions --set final` passes 11/11 on the final set (tool-level, deterministic).

**Next steps.** Server-side table rendering for very large results (the 120-row Q6 table is truncated before reaching the model), a faster hosted model, and a per-claim review UI.

---

## 2. Results — 11 final questions (pages 2–3)

Data: 600 wells, today = 2027-01-15. Figures below are the tool outputs the answers are built from (eval check status in the last column).

| # | Question | Answer / key output | Visual | Eval |
|---|---|---|---|---|
| 1 | Map reservoir pressure | **Cannot be answered.** The dataset has no pressure readings (`data_inventory`). The copilot says so plainly and offers proxies: wellhead-minus-ambient temperature, uptime share. It asks before substituting. | none (honest refusal) | PASS |
| 2 | Top oil producer in 2026 | **Cannot be answered.** There are no production volumes or flow rates. Proxies offered: uptime (producing inspections / all inspections) and downtime days. | none | PASS |
| 3 | Gas-odor notes since 1 Nov 2026: response on time? (OPS-1) | 26 reports: **17 met, 9 missed**. The deadline is 24h before 2027 and 12h from then. A response is a repair job start or a manual (not drone) inspection. Missed: WELL-0163, 0341, **0332** (CTX-0332-004, no response), 0401, 0052, 0362, 0161, 0599, 0516. | table | PASS |
| 4 | Temporary bypasses (OPS-2) | **In place now:** WELL-0248 *Allowed* (MOC-2026-031, extended by MOC-2026-044 to 28 Feb 2027); WELL-0249 *Expired* (MOC-2026-036 ended 15 Dec); WELL-0405 *Not approved*. **Past violations:** WELL-0087 (out 6 Jan) and WELL-0158 (out 28 Dec), both ran past the 15 Dec expiry; WELL-0561 had no approval. Evidence: CTX- notes and MOC chunks. | table | PASS |
| 5 | Regulator letter (NE-7 and NE-8, spills since 1 Jun 2026 of 5 bbl or more) | **6 covered spills, 3 not compliant.** Compliance means the clean-up report says "Soil samples sent to the lab." Compliant: WELL-0119, 0277, 0168. **Not compliant:** WELL-0123 (6.0 bbl), 0182 (5.2), 0262 (6.1). Spills corrected to non-spill are excluded. | table | PASS |
| 6 | Wells where latest inspection and operator notes disagree on producing (OPS-3) | **120 wells field-wide.** The newer record wins. In 10 the note is newer than the inspection; in 110 the inspection is newer. A restart note with no later inspection gives "producing (unconfirmed)". Each row cites both the INS- and CTX- IDs. | table | PASS |
| 7 | Operator-reported abnormal sounds or behaviour with no repair since; top 10, then top 3 for a crew | Ranked by score: WELL-0182 (18.0), 0411 (18.0), 0297 (16.0), 0042 (14.0), 0346 (13.5), 0010 / 0114 (13.0), 0197 / 0263 (12.5), 0175 (12.0). Each has the quoted notes (CTX- IDs) and other warning signs. For example, WELL-0182 has 3 complaints (rattling, tagging bottom, fluid pound), 4 non-producing inspections in 3 months and 6 corrective repairs in 12 months. Turn 2 picks the top 3 from the same ranked data. | table | PASS |
| 8 | Four-session claims and preferences flow | Nathan's WELL-0333 swap claim is stored *unverified* and attributed. Robert still sees WELL-0333 on the recalled list, with the claim flagged and plain-dollar costs, so Nathan's private preference does not leak. Olivia's second-hand WELL-0577 claim is stored *unconfirmed* ("heard from Nathan") and the well stays listed. Nathan's "my area" list (Northwest Block 12 only) is sorted area then well ID, with costs in thousands. | table | PASS |
| 9 | Six-turn session | (1) **9 recalled pumps** still in the ground: Southwest 23 (0031, 0263), Northeast 8 (0156, 0160), Northwest 12 (0305, 0333, 0426, 0428), Southeast 32 (0577). (2) Northwest Production lead is **Nathan Pierce (U-601)** and the team runs **150 wells**, 0 plugged. (3) OPS-5 heat-trace check: 17 cold producing readings, 10 done, **7 missed** (4 in Dec, 3 in Jan), e.g. WELL-0310 and 0354. (4) WELL-0332 odor (CTX-0332-004): **no response and no H2S reading recorded**, so none is given. (5) and (6) follow-ups reuse turn 1's wells for status, last repair and a justified priority pick. | table / text | PASS |
| 10 | What do long shut-ins wait on, by area (2026 jobs) | 474 delayed jobs. Fleet share: Weather 28.1%, Contractors 25.9%, Parts 23.4%, Crews 22.6%. Typical extra delay is **9.0 days**. **Southeast Block 31 stands out:** Contractors 38.5% (+12.6 points vs fleet) and Crews only 7.7% (−14.9 points). | table + stacked bar | PASS |
| 11 | "Which region consistently runs hotter?" (ambiguous) | Both readings are reported and the assumption is stated. **Air temperature** is nearly flat across areas (about 21.0 °C, SW-23 / SW-24 / NE-8 at the top). **Wellhead temperature** is highest in **Southeast Block 31** (about 53.5 °C when producing, 32.6 °C above ambient), ahead of Southeast 32 and Northwest 12. SE-31 is above the fleet mean in 13 months of the period. | table / bar | PASS |

**What worked.** Dedicated ops tools turn each SOP rule into deterministic logic, so answers are reproducible and cited. The honesty tooling handles the two unanswerable questions (Q1, Q2) and the unrecorded H2S reading (Q9). Memory separation held in Q8: claims were attributed, preferences stayed private, and shared data was not silently overridden.

**What did not / limits.**
- Gemma 4 31B on Ollama is slow, so full `--llm` end-to-end runs take minutes per question. Final-set scores above are tool-level; I have not yet run the final set through the model, so tool selection and visual choice there are untested.
- Large results (Q6's 120 rows) are truncated before reaching the model, so a model-written answer may list only part of the table.
- Q7's ranking is a transparent heuristic (complaint count, recent non-producing inspections, repair history). It is explainable but not a trained model.
