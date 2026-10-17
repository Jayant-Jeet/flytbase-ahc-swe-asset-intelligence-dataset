# Regulatory Compliance SOP

> Fictional SOP for the FlytBase Asset Copilot hackathon. Not legal or regulatory guidance.

Document ID: SOP-RC · Revision 2 · Owner: Health, Safety & Environment (TEAM-HSE)

## 1. Purpose

Lists recurring compliance duties for every oil well asset and how they are evidenced in the records.

## 2. Protocols

| Protocol ID | Requirement | Evidence in the data |
|---|---|---|
| **RC-01** | Every well must be inspected at least once every **14 days**. | `inspections.jsonl` timestamps |
| **RC-02** | Every flowline must pass an **integrity test once per calendar year**. | `maintenance.jsonl` entries with issue "Annual flowline integrity test" |
| **RC-03** | Any well down for more than **14 consecutive days** must be reported to the area production supervisor. | Inspections plus context notes |
| **RC-04** | Agency notices of violation must be answered within **30 days** and tracked until closed. | `regulatory-events.jsonl` status and `nov_response_filed` events |
| **RC-05** | Keep a **flowline maintenance program**: weekly visual inspection of the pad and flowline route, corrective repair of any leak found, and prompt cleanup. Modelled on EPA SPCC rules for onshore production (40 CFR 112.9(d)(4)). | Inspections, flowline repairs and their reports |

## 3. External agencies

| Agency | Typical events |
|---|---|
| State Oil & Gas Commission | Spill reports, notices of violation, fines |
| Regional Environmental Protection Office | Site inspections, remediation verification |

## 4. Status values

Regulatory events are `open` until the agency confirms closure, then `closed`.
