# Test Dataset: Release Notes

This is the test dataset for the final 2 hours. It uses the same folder layout and file
formats as the sample data. Load it, then run the questions in `final-questions.md`.

**"Today" is 15 Jan 2027.** "Right now" means each well's current state as of today.

## What is in it

| | Sample data | Test data |
|---|---|---|
| Wells | 200 | 600 |
| Areas | 4 | 7 |
| Production teams | 2 | 4 |
| Data up to | 16 Oct 2026 | 15 Jan 2027 |

- The 200 sample wells are all here, with three more months of records. Some older
  records have also been updated since the sample was taken, as happens in a live system.
- 400 more wells have been added, with their full history. The new areas include
  **Southeast Block 31**, **Northwest Block 12** and **Southeast Block 32**, and there is a
  new team, **Northwest Production**. The people are in `fleet/users.json`.

## New documents in `fleet/sops/`

| File | What it is |
|---|---|
| `operations-sop-rev2.md` | Operations SOP, revision 2 (effective 1 Oct 2026) |
| `supplier-recall-notice-2026-12-01.md` | A recall notice from a pump supplier |
| `moc-2026-031.md`, `moc-2026-036.md`, `moc-2026-044.md` | Management-of-change (MOC) memos: approvals for temporary changes to how wells are run |
| `regulator-letter-2026-11-20.md` | A letter from the State Oil & Gas Commission |

## Operator notes

`context.jsonl` now also carries notes written by the field operators during their shifts
(`note_type` = `operator_note`, written by the operator on shift). `created_at` is when the
note was written. These notes describe what the operators saw and did at the well.

## New sections in repair reports

Some repair reports have these lines, before "Work done":

| Section | Meaning |
|---|---|
| `**Parts installed:**` | The pump fitted on this job: model, serial number and manufacturing lot. |
| `**Follow-up:**` | The crew will come back within a number of days to do something. A later job that does it says so in its `issue` ("Follow-up to MNT-…"). |

## New fields

None of these fields appears in every record. Treat them as optional.

| File | Field | Meaning |
|---|---|---|
| `metadata.json` | `well_type` | Can now also be `T500` (see `fleet/well-types/T500.md`) |
| `metadata.json` | `type_history` | The well's type over time: a list of `well_type`, `from`, `to` (`to` is empty for the current type) |
| `metadata.json` | `team_history` | The team that ran the well over time: a list of `team_id`, `from`, `to`. A well's own `team_id` and `team_history` take priority over its area's team in `areas.json`, so one area can have wells run by two teams |
| `metadata.json` | `status`, `decommissioned_on` | `plugged_and_abandoned` and the date, for wells that have been permanently closed |
| `metadata.json` | `recorded_status` | Can now also be `plugged` |
| `inspections.jsonl` | `recorded_at` | When an inspection was entered, if it was entered late. Its ID ends in `-B` followed by a number |
| `maintenance.jsonl` | `recorded_at` | When a job was entered, if it was entered late. Its ID ends in `-L` followed by a number |
| `maintenance.jsonl` | `cost_revision` | The original total, the date it was revised, and why. `cost_usd` holds the revised amounts |
| `context.jsonl` | `note_type` = `data_correction` | A correction to one field of one inspection: `inspection_id`, `field`, `original_value`, `corrected_value`, and the reason in `text` |

Some jobs were still in progress on 15 Jan 2027. Their `completed_at` is the planned finish date, which can be after today, and their report is an interim report.

A late-entered record can appear at the end of its file, out of time order. Use its dates,
not its position in the file.

## New equipment kind

`esp_pump`: an electric submersible pump. Its equipment record covers the downhole motor,
seal section, pump, power cable and surface drive.
