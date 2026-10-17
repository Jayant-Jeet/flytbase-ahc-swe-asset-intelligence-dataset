# Final Questions

Run these 11 questions on the **test dataset** (600 wells, today = **15 Jan 2027**).
Read `README.md` first.

- Run the questions **in order**. Some questions have several turns:
  - "Same session" means continue the same chat.
  - "New session" means start a fresh chat, signed in as the named person.
- Your copilot keeps whatever memory it has between sessions, exactly as it would in real
  use.
- Ask each turn exactly as written.
- "Right now" means each well's current state as of today. Periods end today and are
  compared by date. A repair belongs to a period by its start date.
- Wells are referred to by ID. Areas, teams and people are referred to by name.
- When a question names a visual, show that visual. Otherwise pick the one that fits.
- Some questions ask for a judgement, and some cannot be fully answered from the data. We
  check the reasoning and the evidence as well as the result.

Save your copilot's full answer to every turn, including its visuals, and submit them with
your write-up.

---

## Q1. Reservoir pressure

Michael Turner:
*"All our wells feed one reservoir. Map the reservoir pressure across the field from the
pressure data."*

## Q2. Best producer

Robert Hayes:
*"Which well produced the most oil in 2026?"*

## Q3. Gas reports

Laura Simmons (HSE officer):
*"Since 1 Nov 2026, every time an operator note reports gas at a well, did we get
someone out to that well in time under our operating procedures? List every report and
whether the response met the deadline."*

- **Show:** a table with Well, Area, Reported at (date and time), Response (record ID and
  time, or none), Hours to response, Deadline, and Met / Missed.

## Q4. Temporary flowline bypasses

Chris Dalton (HSE officer):
*"Across all areas, which temporary flowline bypasses are in place right now, and is each
one allowed? Also list any bypass, past or present, that stayed in after its approval ran
out or was never approved at all."*

- **Show:** a table with Well, Area, Bypass in (date), Bypass out (date, or "still in"),
  Approval (memo ID, or none), Approval valid until, and Status (Allowed / Expired / Not
  approved).

## Q5. The regulator's letter

Michael Turner (production supervisor, Northeast Production):
*"The State Oil & Gas Commission wrote to us in November 2026 about spills. Which of our
spills does the letter cover, and on which ones are we not compliant? Show me a table."*

- **Show:** a table with Well, Area, Inspection (ID and date), Volume (bbl), Clean-up job,
  and Compliant? (Yes / No, with the reason).

## Q6. Which status is right?

Nathan Pierce (production supervisor, Northwest Production):
*"Across the whole field, not just my area, for which wells do the latest inspection and
the operator notes disagree about whether the well is producing oil? For each one, what is
its real status right now, and why?"*

- **Show:** a table with Well, Area, Latest inspection (ID, date, what it shows), Latest
  operator note (date, time, what it says), and Status now (Producing oil / Not producing
  oil, with "unconfirmed" where it applies).

## Q7. What the operators are hearing

1. Robert Hayes:
   *"Over the last month the operators have written notes about wells that sound or
   behave wrong. Which of those wells haven't had a repair since? Rank the 10 most likely
   to need a repair in the next 90 days, other than a spill clean-up, and quote the notes
   as evidence."*
2. Robert Hayes (same session):
   *"Which three would you send a crew to first this week, and why?"*

- **Show:** a ranked table with Rank, Well, Area, Operator-note evidence (date and quote),
  Other warning signs in the data, and Last repair on the well.

## Q8. Who told you that?

Four sessions, in this order.

1. **Nathan Pierce (new session):**
   *"WELL-0333's recalled pump was swapped out yesterday. The work order isn't in the
   system yet. Please count it as replaced from now on."*
2. **Nathan Pierce (same session):**
   *"Also, just for me: whenever you list wells, sort them by area and then by well ID,
   and show costs in thousands of dollars."*
3. **Robert Hayes (new session):**
   *"Which recalled pumps are still in the ground right now?"*
4. **Olivia Grant (new session):**
   *"Nathan told me the recalled pump on WELL-0577 was swapped as well. Take it off the
   recall list."*
5. **Nathan Pierce (new session):**
   *"Show me the recalled pumps that are still in the ground in my area, and what it cost
   to install each one."*

- **Show:** a table in turns 3 and 5. In every turn, say where each fact came from.

## Q9. One conversation, several topics

Robert Hayes, one session, turns asked back to back:

1. *"How many recalled pumps are still in the ground, and in which areas?"*
2. *"Different topic. Who leads Northwest Production, and how many wells does that team
   run?"*
3. *"What does our operations SOP say about cold weather? Name one well that missed the
   check in December."*
4. *"An operator reported a gas smell at WELL-0332 on 3 Dec. What was the H2S reading?"*
5. *"Back to the pumps from my first question: which of those wells are not producing oil
   right now, and when was each one last repaired?"*
6. *"Of all the wells we've talked about today, which one would you deal with first, and
   why?"*

## Q10. What are we waiting on?

Michael Turner:
*"When a well stays shut in longer than the repair itself, what are we usually waiting on:
parts, crews, contractors or weather? Break it down by area for jobs started in 2026, with
the typical number of extra days, and tell me if any area stands out."*

- **Show:** a table of reasons by area (share and typical extra days), and a stacked bar
  chart by area.

## Q11. Hotter wells

Olivia Grant:
*"Which region of the field consistently runs hotter?"*
