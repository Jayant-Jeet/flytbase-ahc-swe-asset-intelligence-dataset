# Spill Response SOP

> Fictional SOP for the FlytBase Asset Copilot hackathon. Simplified and modelled on
> common US onshore practice. It is not a copy of any rule and is not legal or regulatory
> guidance.

Document ID: SOP-SR · Revision 5 · Effective 1 Jan 2027 · Owner: Health, Safety & Environment (TEAM-HSE)

## Revision history

| Revision | In force for spills discovered | Change |
|---|---|---|
| 5 | from 1 Jan 2027 | SR-01 written notice deadline cut from 24 hours to **12 hours** |
| 4 | before 1 Jan 2027 | SR-01 written notice within 24 hours |

Use the revision that was in force on the date the spill was **discovered**.

## 1. Purpose

Defines how field teams respond to, classify, report and close out crude oil
releases on well pads and along flowlines.

## 2. Background (industry references)

- **Reporting:** state oil and gas regulators commonly require immediate notice of fires,
  leaks, spills and breaks, and a written report for crude oil losses of more than 5
  barrels. Texas Railroad Commission Statewide Rule 20 and its Form H-8 are an example. Our SR-01 keeps the
  immediate phone call and adds a company deadline for the written notice (12 hours from
  revision 5; 24 hours before).
  Our reporting threshold (5 bbl or more) is slightly stricter than "more than 5 bbl".
- **Cleanup:** Texas Statewide Rule 91 is an example cleanup standard for crude oil on
  land in non-sensitive areas. It requires free oil to be removed promptly, the
  contamination to be mapped, and soil cleaned to **1.0% total petroleum hydrocarbons
  (TPH) by weight**; Commission guidance also describes treating more heavily
  contaminated soil (around 5% TPH) by mixing it down or hauling it away. Our SR-03 follows
  this approach. The number of soil samples and the 60-day verification deadline in SR-03
  are company rules.
- **Prevention:** for onshore production facilities covered by EPA's SPCC rules,
  40 CFR 112.9 requires a flowline maintenance program and prompt cleanup of discharges
  (see Regulatory Compliance SOP, RC-05).

## 3. Spill classes

The classes, the 72-hour HSE notice and the 4-hour containment limit (SR-02) are company
rules.

| Class | Estimated crude volume | Notes |
|---|---|---|
| Minor | below 1 bbl | Record and clean up; no external report |
| Moderate | 1 bbl to below 5 bbl | Record, clean up, notify HSE within 72 h |
| Reportable | 5 bbl to below 25 bbl | External report required (SR-01) |
| Major | 25 bbl or more | External report within 2 h (company rule), incident command |

1 bbl (barrel) = 159 litres.

## 4. Protocols

| Protocol ID | Requirement |
|---|---|
| **SR-01** | For a **reportable or major** spill, phone the State Oil & Gas Commission immediately, then file the **written notice** (volume, affected area, cleanup plan) within **12 hours** of discovery (24 hours for spills discovered before 1 Jan 2027), or within **2 hours** for a major spill. Discovery time is the time of the inspection that detected the spill. `hours_to_report` in the data is the time to the written notice. |
| **SR-02** | The source must be stopped and the spill contained within **4 hours** of discovery. |
| **SR-03** | Contaminated soil must be cleaned to **1.0% TPH or less**. Take **at least 4 soil samples** to map the edges of a reportable spill. Haul soil above 5% TPH to an approved disposal site. For reportable and major spills, the remediation must be **verified within 60 days** of discovery; verification is closed only by a passing site inspection from the environmental agency. Smaller spills are cleaned to the same 1.0% TPH target without agency verification. |
| **SR-04** | Every spill, including minor ones, must be recorded against the asset with an estimated volume and affected area. |

## 5. Response steps

1. Stop the source (shut in the well if needed; see the well-type manual).
2. Contain the spill (berms, absorbents).
3. Estimate volume and affected pad area.
4. Classify using section 3.
5. Report per SR-01 if required.
6. Repair the cause and log a corrective maintenance record.
7. Remediate, sample the soil and request agency verification (SR-03).
8. Write the maintenance report with a compliance check (Field Work SOP, WS-07).

## 6. Penalties

A late SR-01 report usually results in a **notice of violation** from the State Oil &
Gas Commission, typically with a fine between USD 2,500 and 7,500. An open notice
stays on the asset's compliance record until the agency closes it.

## 7. False positives

If a detected "spill" is later confirmed as water, mud or shadow by a qualified
person, that correction is recorded as a context note. Corrected detections do not
count as spills for reporting or statistics.
