"""Tool-level checks and end-to-end scenarios for the 11 final questions (test-data/final-questions.md).

Run with:  python -m tests.eval_questions --set final [--llm]
Expected values are properties of the final test dataset (600 wells, today = 2027-01-15).
"""
from __future__ import annotations

NATHAN, ROBERT, OLIVIA, LAURA, CHRIS, MICHAEL = ("Nathan Pierce", "Robert Hayes", "Olivia Grant", "Laura Simmons",
                                                  "Chris Dalton", "Michael Turner")


def _rows(r, key="rows"):
    return {x["well_id"]: x for x in r.get(key, [])}


def f1(c):
    r = c.tool("data_inventory", {}, ROBERT)
    return [("pressure listed as NOT in the dataset", any("pressure" in x for x in r["not_in_dataset"])),
            ("proxies offered", bool(r["proxies"]))]


def f2(c):
    r = c.tool("data_inventory", {}, ROBERT)
    return [("oil volumes listed as NOT in the dataset", any("production" in x for x in r["not_in_dataset"]))]


def f3(c):
    r = c.tool("gas_odor_responses", {"since": "2026-11-01"}, LAURA)
    w = _rows(r)
    n = w.get("WELL-0332", {})
    return [("reports found", r["reports"] > 0 and r["reports"] == len(r["rows"])),
            ("met + missed adds up", r["met"] + r["missed"] == r["reports"]),
            ("WELL-0332 odor has no response and is Missed", n.get("response_id") is None and n.get("result") == "Missed"),
            ("deadline is 24h before 2027 (from SOP)", all(x["deadline_hours"] == 24 for x in r["rows"] if x["reported_at"] < "2027-01-01")),
            ("each row has a note id", all(x["note_id"].startswith("CTX-") for x in r["rows"]))]


def f4(c):
    r = c.tool("temporary_bypasses", {}, CHRIS)
    w = {x["well_id"]: x for x in r["all_episodes"]}
    return [("WELL-0248 allowed via extension MOC-2026-044", w["WELL-0248"]["status"] == "Allowed" and "044" in w["WELL-0248"]["approval"]),
            ("WELL-0249 expired (MOC-2026-036 ended 15 Dec)", w["WELL-0249"]["status"] == "Expired"),
            ("WELL-0405 never approved", w["WELL-0405"]["status"] == "Not approved"),
            ("past episode WELL-0087 flagged expired", w["WELL-0087"]["status"] == "Expired" and w["WELL-0087"]["bypass_out"] != "still in")]


def f5(c):
    r = c.tool("regulator_letter_spills", {}, MICHAEL)
    rows = r["rows"]
    return [("covers only NE-7 / NE-8 spills >= 5 bbl", all(x["volume_bbl"] >= 5 for x in rows) and
             {x["area"] for x in rows} <= {"Northeast Block 7", "Northeast Block 8"}),
            ("some compliant and some not", 0 < r["not_compliant"] < len(rows)),
            ("each row names a reason", all(x["reason"] for x in rows))]


def f6(c):
    r = c.tool("status_conflicts", {"scope": "all"}, NATHAN)
    w = _rows(r)
    return [("fleet-wide (several areas)", len({x["area"] for x in r["rows"]}) > 3),
            ("newer note and newer inspection both handled", r["note_newer_than_inspection"] > 0 and r["inspection_newer_than_note"] > 0),
            ("restart note newer than inspection -> producing (unconfirmed)",
             any(x["status_now"] == "Producing oil (unconfirmed)" for x in w.values())),
            ("every row has both record ids", all(len(x["evidence_ids"]) == 2 for x in r["rows"]))]


def f7(c):
    r = c.tool("operator_complaints", {}, ROBERT)
    t = r["top"]
    return [("10 ranked wells", len(t) == 10),
            ("each has quoted notes with ids", all(x["notes"] and x["notes"][0]["id"].startswith("CTX-") for x in t)),
            ("scores descending", [x["score"] for x in t] == sorted((x["score"] for x in t), reverse=True))]


def f8(c):
    out = []
    m = c.tool("remember_fact", {"scope_type": "well", "scope_id": "WELL-0333", "text": "Recalled pump swapped yesterday; work order not in system",
                                 "kind": "pump_replaced"}, NATHAN)
    out.append(("Nathan's claim stored, attributed, unverified", "error" not in m and m.get("note") is not None))
    p = c.tool("set_preference", {"key": "sort_order", "value": "area,well_id"}, NATHAN)
    c.tool("set_preference", {"key": "cost_unit", "value": "thousands"}, NATHAN)
    out.append(("preference saved privately", "error" not in p))
    rb = c.tool("recalled_pumps", {"scope": "all"}, ROBERT)
    w = _rows(rb)
    out += [("Robert: WELL-0333 still listed", "WELL-0333" in w),
            ("Robert: Nathan's claim shown, flagged unverified", any("Nathan" in str(x) for x in w["WELL-0333"]["user_claims"])),
            ("Robert: plain dollars (Nathan's pref not leaked)", w["WELL-0333"]["install_cost_usd"] > 1000 and
             "cost_unit" not in str(rb.get("display", "")))]
    mo = c.tool("remember_fact", {"scope_type": "well", "scope_id": "WELL-0577", "text": "Nathan said the recalled pump was swapped",
                                  "kind": "pump_replaced", "second_hand": True, "heard_from": "Nathan Pierce"}, OLIVIA)
    out.append(("Olivia's second-hand claim stored unconfirmed", "unconfirmed" in str(mo).lower()))
    rb2 = c.tool("recalled_pumps", {"scope": "all"}, ROBERT)
    out.append(("WELL-0577 stays on the list", "WELL-0577" in _rows(rb2)))
    rn = c.tool("recalled_pumps", {"scope": "my area"}, NATHAN)
    ids = [x["well_id"] for x in rn["rows"]]
    out += [("Nathan: only Northwest wells", {x["area"] for x in rn["rows"]} == {"Northwest Block 12"}),
            ("Nathan: sorted by area then well id", ids == sorted(ids)),
            ("Nathan: costs in thousands", "thousand" in str(rn).lower())]
    return out


def f9(c):
    out = []
    n = c.tool("recalled_pumps", {"scope": "all"}, ROBERT)
    out.append(("9 recalled pumps, several areas", n["count"] == 9 and len({x["area"] for x in n["rows"]}) > 1))
    t = c.tool("team_info", {"team": "Northwest Production"}, ROBERT)
    out.append(("Northwest lead Nathan Pierce, wells counted", t["lead"] == "Nathan Pierce" and t["wells_run_today"] > 0))
    f = c.tool("freeze_protection_check", {"since": "2026-12-01", "until": "2026-12-31"}, ROBERT)
    out.append(("a December well missed the heat trace check", f["missed"] > 0 and f["missed_examples"][0]["status"] == "Missed"))
    g = c.tool("gas_odor_responses", {"since": "2026-12-03", "until": "2026-12-03"}, ROBERT)
    r = _rows(g).get("WELL-0332", {})
    out.append(("WELL-0332: no response recorded, so no H2S reading tied to it", r.get("response_id") is None))
    return out


def f10(c):
    r = c.tool("repair_delay_reasons", {"year": 2026}, MICHAEL)
    reasons = {s["name"] for s in r["stacked_bar_series"]}
    return [("reasons grouped into the four classes", reasons <= {"Parts", "Contractors", "Crews", "Weather"} and len(reasons) == 4),
            ("table has share and typical extra days", all("share_pct" in x and "typical_extra_days" in x for x in r["table"])),
            ("area deviations reported", bool(r["largest_deviations_from_fleet"]))]


def f11(c):
    r = c.tool("area_temperature_comparison", {"period": "last_12_months"}, OLIVIA)
    return [("both ambient and wellhead reported", {"ambient_air_temp_c", "wellhead_temp_c_producing_only"} <= set(r["metrics"])),
            ("assumption stated", "ambiguous" in r["assumption_note"])]


FINAL_TOOL_CHECKS = {1: f1, 2: f2, 3: f3, 4: f4, 5: f5, 6: f6, 7: f7, 8: f8, 9: f9, 10: f10, 11: f11}

# (user, message, expected visual, required strings (alternatives as lists), new_chat)
_S = [
    (1, [(MICHAEL, "All our wells feed one reservoir. Map the reservoir pressure across the field from the pressure data.", None,
          [["no pressure", "not in the dataset", "does not contain", "doesn't contain", "no reservoir pressure"]], False)]),
    (2, [(ROBERT, "Which well produced the most oil in 2026?", None,
          [["no production", "not in the dataset", "does not contain", "doesn't contain", "no oil volume"]], False)]),
    (3, [(LAURA, "Since 1 Nov 2026, every time an operator note reports gas at a well, did we get someone out to that well in time under our operating procedures? "
                 "List every report and whether the response met the deadline.", "table", ["WELL-0332", "Missed"], False)]),
    (4, [(CHRIS, "Across all areas, which temporary flowline bypasses are in place right now, and is each one allowed? Also list any bypass, past or present, "
                 "that stayed in after its approval ran out or was never approved at all.", "table", ["WELL-0249", "WELL-0405", "Expired", "Not approved"], False)]),
    (5, [(MICHAEL, "The State Oil & Gas Commission wrote to us in November 2026 about spills. Which of our spills does the letter cover, and on which ones are we not "
                   "compliant? Show me a table.", "table", ["WELL-0123", "Soil samples"], False)]),
    (6, [(NATHAN, "Across the whole field, not just my area, for which wells do the latest inspection and the operator notes disagree about whether the well is "
                  "producing oil? For each one, what is its real status right now, and why?", "table", ["WELL-0026", "unconfirmed"], False)]),
    (7, [(ROBERT, "Over the last month the operators have written notes about wells that sound or behave wrong. Which of those wells haven't had a repair since? "
                  "Rank the 10 most likely to need a repair in the next 90 days, other than a spill clean-up, and quote the notes as evidence.", "table", ["WELL-0182", "CTX-"], False),
         (ROBERT, "Which three would you send a crew to first this week, and why?", None, ["WELL-"], False)]),
    (8, [(NATHAN, "WELL-0333's recalled pump was swapped out yesterday. The work order isn't in the system yet. Please count it as replaced from now on.", None,
          [["unverified", "not verified", "can't verify", "cannot verify", "not in the system"]], True),
         (NATHAN, "Also, just for me: whenever you list wells, sort them by area and then by well ID, and show costs in thousands of dollars.", None,
          [["only you", "just for you", "private", "saved"]], False),
         (ROBERT, "Which recalled pumps are still in the ground right now?", "table", ["WELL-0333", "Nathan", ["unverified", "not verified", "claim"]], True),
         (OLIVIA, "Nathan told me the recalled pump on WELL-0577 was swapped as well. Take it off the recall list.", None,
          [["unconfirmed", "second-hand", "secondhand", "hearsay"], "Nathan"], True),
         (NATHAN, "Show me the recalled pumps that are still in the ground in my area, and what it cost to install each one.", "table", ["WELL-0333", ["thousand", "k"]], True)]),
    (9, [(ROBERT, "How many recalled pumps are still in the ground, and in which areas?", None, ["9"], True),
         (ROBERT, "Different topic. Who leads Northwest Production, and how many wells does that team run?", None, ["Nathan Pierce", "150"], False),
         (ROBERT, "What does our operations SOP say about cold weather? Name one well that missed the check in December.", None, ["heat trace", "WELL-0310"], False),
         (ROBERT, "An operator reported a gas smell at WELL-0332 on 3 Dec. What was the H2S reading?", None,
          ["CTX-0332-004", ["no response", "no recorded", "not recorded", "no h2s", "none"]], False),
         (ROBERT, "Back to the pumps from my first question: which of those wells are not producing oil right now, and when was each one last repaired?", None, ["WELL-"], False),
         (ROBERT, "Of all the wells we've talked about today, which one would you deal with first, and why?", None, ["WELL-"], False)]),
    (10, [(MICHAEL, "When a well stays shut in longer than the repair itself, what are we usually waiting on: parts, crews, contractors or weather? Break it down by area for jobs "
                    "started in 2026, with the typical number of extra days, and tell me if any area stands out.", "bar_chart", ["Southeast Block 31", "Contractors"], False)]),
    (11, [(OLIVIA, "Which region of the field consistently runs hotter?", None, [["ambient", "air temperature"], "wellhead"], False)]),
]

FINAL_SCENARIOS = {q: [dict(user=u, msg=m, visual=v, has=[list(h) if isinstance(h, (list, tuple)) else [h] for h in has],
                            lacks=[], tools=[], new_chat=nc, next_day=False, table_lacks=[]) for u, m, v, has, nc in turns]
                   for q, turns in _S}
