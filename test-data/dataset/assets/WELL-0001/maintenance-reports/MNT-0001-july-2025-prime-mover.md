# Maintenance Report — MNT-0001-021

**Asset:** WELL-0001 (SW-23 Well 001, T200, Southwest Block 23)
**Equipment:** pump jack
**Work started:** 25 Jul 2025 08:00 · **Finished:** 26 Jul 2025 15:00 · **Well stopped for this job:** 31 hours
**Crew lead:** Kevin Price (U-303) · **Crew:** Daniel Foster
**Triggered by:** inspection INS-0001-0140 on 2025-07-24
**Report written:** 26 Jul 2025

## What happened

On a very hot afternoon (air temperature 43.1 °C) the electric motor driving the pump jack
tripped on thermal overload and the pump jack stopped. The drone thermal reading during
inspection INS-0001-0140 showed the wellhead at 51.6 °C. That looks hot, but it was only
8.5 °C above the air temperature. For a T200 well that means the well was **not
producing**: the warmth was from the sun, not from oil flowing.

## Root cause

The motor's cooling fan shroud was cracked and was restricting airflow. In extreme heat
the motor could not shed enough heat and the overload protection tripped, as designed.

## Safety and permits

- Pre-job safety meeting held with the crew before work started (WS-01).
- Energy isolated: brake set and chained, motor disconnect locked and tagged (WS-02).
- Personal H2S monitors worn; highest reading 0 ppm (WS-03).
- No hot work needed.

## Work done

1. Checked the motor windings with an insulation tester: no damage found.
2. Removed the cracked cooling fan shroud and fitted a new one.
3. Cleaned dust from the cooling fins.
4. Reset the motor overload.

Summary of the fix: reset motor overload, replaced cooling fan shroud.

## Testing and return to service

Ran the unit for 2 hours: motor current within the nameplate rating and stroke rate
normal. The well was returned to production.

## Parts and cost

| Item | USD |
|---|---|
| Fan shroud | 450 |
| Consumables | 190 |
| Labour (about 11 crew-hours) | 900 |
| **Total** | **1,540** |

## Compliance check

| Protocol | Result | Detail |
|---|---|---|
| WS-01 | Met | Meeting held and recorded |
| WS-02 | Met | Energy isolated and tagged |
| WS-03 | Met | Monitors bump-tested; max 0 ppm |
| WS-07 | Met | Written the same day the work finished |

## Notes and recommendations

- Check motor cooling on all pump jacks in this area before next summer.
- Remind operators that a hot wellhead on a hot day does not mean the well is producing; compare with the air temperature.
