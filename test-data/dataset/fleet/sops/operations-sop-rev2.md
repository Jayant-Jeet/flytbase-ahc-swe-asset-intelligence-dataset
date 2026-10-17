# Operations SOP

> Fictional SOP for the FlytBase Asset Copilot hackathon. Simplified; it is not real operating,
> safety or regulatory guidance.

Document ID: SOP-OPS · Revision 2 · Effective 1 Oct 2026 · Owner: Field Operations with Health, Safety & Environment (TEAM-HSE)

## Revision history

| Revision | Effective | Change |
|---|---|---|
| 2 | 1 Oct 2026 | Adds OPS-1 to OPS-6. OPS-1 response time is cut from 24 hours to 12 hours for reports from 1 Jan 2027. |
| 1 | 1 Mar 2025 | First issue (shift handover only). |

## Operator notes

Field operators record what they see on their rounds as operator notes: the records with
`note_type` = `operator_note` in the well's notes (`assets/<WELL_ID>/context.jsonl`). The note's
`created_at` is the time of the observation (UTC, like the other timestamps in the records). A
round note is filed under the well it is about (`asset_id`) and names that well by its ID in its
text (for example WELL-0412).

## OPS-1 Gas odor response

An operator note reporting a gas smell needs a field response on that well. A gas odor report is an
operator note saying the crew smelled gas, noticed a rotten-egg smell or a gas odor, or that an H2S
monitor alarmed at a well.

A field response is either:
- a maintenance job on that well (its `started_at` in `maintenance.jsonl`), or
- an inspection of that well with `method` = `manual`. Drone inspections do not count.

The response must start after the note's `created_at` and within:
- **24 hours** for reports noted before 1 Jan 2027;
- **12 hours** for reports noted from 1 Jan 2027.

Otherwise the response is missed. Crews on site follow WS-03 of the Field Work SOP (H2S monitors,
10 ppm stop-work level).

## OPS-2 Temporary bypass

A temporary flowline bypass is allowed only on a well named in a management-of-change memo
(the `moc-*.md` files in `fleet/sops/`) that has not expired. A memo that extends an earlier memo
replaces its expiry date.

A bypass is in place from the operator note that puts it in until the operator note that takes it
out.
It is a violation if:
- the well is not named in any MOC, or
- the bypass is still in place after the expiry date (the end of that day), including a bypass that
  is still in place today.

## OPS-3 Status evidence precedence

A well's current status comes from the most recent of these two:
- its latest inspection reading (producing oil or not producing oil, by the well-type manual,
  after any `data_correction` note), or
- its latest operator note saying the well was shut in or restarted.

If a shut-in note is more recent than the latest inspection, the well is **not producing (shut
in)**, even if that inspection reads as producing. If a restart note is more recent than the latest
inspection, the well is **producing (unconfirmed)** until an inspection confirms it. Other operator
notes (gas odor, bypass, equipment remarks) do not change the status.

## OPS-4 Failure classification

Classify a corrective job from the "## Root cause" section of its maintenance report. Check the
rows of this table **in order**: the **first** row with a keyword that appears in the root-cause
text (ignoring upper and lower case) gives the category. Order matters: a text that mentions both
sand and wear is Sand, and a text that mentions both corrosion and fatigue is Corrosion.

| Order | Category | Keywords |
|---|---|---|
| 1 | Installation error | installation, installed incorrectly, misaligned, re-terminated, not torqued, cross-threaded |
| 2 | Sand | sand |
| 3 | Scale | scale, paraffin |
| 4 | Gas interference | gas lock, gas interference, free gas |
| 5 | Corrosion | corrosion, corroded |
| 6 | Electrical | winding, insulation, switch, terminal box |
| 7 | Fatigue/wear | fatigue, wear, worn, years of service, buckling, ageing, vibration |
| 8 | Other | no keyword above matched |

## OPS-5 Freeze protection (1 Dec – 28 Feb)

From 1 Dec to 28 Feb, any inspection with `ambient_temp_c` at or below **−5 °C** on a well that is
producing oil at that reading needs a heat trace check on that well: a maintenance job whose `issue`
contains "Heat trace check", starting within **7 days** after the inspection. A cold reading on a
well that is not producing oil needs no check.

## OPS-6 Repeat failure

A corrective job is a **repeat failure** if it is on the same equipment kind of the same well and
it starts within **30 days** after the previous corrective job on that equipment was completed.

A repeat failure counts against the crew that did the previous job only when the repeat job's
root cause (OPS-4) is **Installation error**. Count it against the previous job's contractor; a
previous job with no contractor counts against the company crew.

See also the Spill Response SOP (reportable spills and clean-up) and the Field Work SOP (WS-01 to
WS-07) for how the work itself is done and reported.
