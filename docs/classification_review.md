# Phase 2 classification: sample review (2026-09-24)

**Method.** Claude read `docs/classification_sample.csv`, 100 random cases per tier with phone numbers, emails and 5+ digit strings removed. The file is gitignored and stays on quad. The question for each case was: *does it indicate water standing on or failing to drain from a street, or a street-side drainage failure, at that location?*

## Round 1 (initial rules)

| Tier | Estimated precision | Notes |
|---|---|---|
| strong | ~85% | Mostly `Flooding` and the 2011–14 `Low Area or Standing Water ROW` type. Errors: "please disregard / wrong SR", a water-main break, a private parking lot, and SeeClickFix reports of "Debris at curb" filed as Flooding (Harvey cleanup). |
| moderate | ~40–50% | 63 of 100 are 2011–14 types whose only text is the address. Those are judged by type (inlet clogged, ditch maintenance). `Drainage` records with text are mostly ditch mowing, cleaning or regrading; collapsed or missing inlet covers; objects dropped into drains. That is drainage upkeep more than flooding. |
| weak | ~15% | "ditch" and "storm drain" matched weeds, trash, dropped phones and keys, and stop signs in ditches. "Flood" in unrelated types (tree trim, containers, fountains, CIP requests) was nearly always noise. |

## Changes made (in `classify/rules.yaml`)
1. **Void patterns** drop "disregard", "wrong SR" and "entered in error" records (118 cases).
2. **Demote patterns** lower maintenance-only text one tier unless a strong keyword also matches: weeds/grass/mowing, trash/dumping/tires, "debris at curb", dropped phones/keys/wallets, missing or broken covers, water-main breaks (5,357 cases).
3. **Weak keywords** for ditch/culvert/storm drain/inlet now require a water or blockage word within 60 characters.
4. **Rule 6 disabled** (unrelated type plus a "flood" keyword). It can be re-enabled by setting `other_type_strong_tier`.

Result: 182,727 → 163,507 signals. Strong 36,544, moderate 118,091, weak 8,872.

## Round 2 (after tuning)

| Tier | Estimated precision | Notes |
|---|---|---|
| strong | ~90% | Remaining doubt: `Flooding` records with no text, and yard or home flooding (area signal, not street). |
| moderate | ~45–55% | Unchanged in character: dominated by `Drainage` (98k) and the 2011–14 ditch types. These are real drainage-system complaints, but only some mean the street floods. |
| weak | ~50% | Now mostly street ponding (`Street Condition`, `Street Hazard`) and demoted `Drainage` records about blocked or trashed ditches. |

The brief's default weights (strong 3, moderate 1.5, weak 0.5) look reasonable against these precisions. Two alternatives for Phase 4:
- **Option A:** keep the weights but let the storm-day event-recurrence factor carry most of the signal. Drainage calls on dry days are mostly maintenance; the same hex calling in during several storms is the real indicator.
- **Option B:** lower moderate to about 1.0.

## Open items for the user
- **Storm events (resolved 2026-09-25):**
  - 2019-09-23..26 late Imelda reports are merged into the Imelda event (merge window 2019-09-18..30).
  - 2012-01-09: flash flooding across the metro (Oak Forest / Brickhouse Gully, Richmond Ave stalls).
  - 2018-07-04: 6–8 in near I-10 and the 610 Loop, hundreds of stranded motorists.
  - 2024-07-26: overnight tropical rain, 5–7 in around Dickinson and Baytown.
  - All three are now labelled in `known_events`.
  - Beryl (2024-07-08) is still not detected: 67/day against a threshold of 153, most likely because of the power outage.
- **Privacy:** redaction does not catch personal names or house numbers inside free text ("Constituent <name>", "contact Mrs. <name>"), so the sample file must stay local. Nothing from it is committed.
- **Title field:** it is only "<type>" or "<type> - <case#> - <address>", so it is excluded from keyword matching.
