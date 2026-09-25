## Key findings (read this first)

Data pulled 2026-09-24: 19 files, 4.2 GB, 5.69M records (5.59M in YTD files 2011–2026, plus 77.6k Harvey). Hashes are in `data/raw/manifest.tsv`.

### Schemas
- **All 16 D365 files (2011–2026 YTD, and MTD) share one 49-column schema.** The city regenerated the older years from D365, so there is no legacy schema to map apart from Harvey.
- **Both Harvey files use the pre-D365 29-column schema.** `Harvey.txt` is the space-padded fixed-width version of `Harvey-clean.txt`: same rows and same values. Only the clean file needs loading.
- **Harvey adds little:** 74,306 of its 77,592 cases already appear in the 2017 YTD file under the same case number. It adds about 3,300 cases and has **no free text at all**.
- **Parse quality is good.** There are 9–117 malformed rows per file (≈370 in total), 11–12 multi-line records per file, and zero non-UTF-8 lines, so no cp1252 fallback is needed.
  - The "too many fields" rows come from stray pipes in the trailing free-text columns. Rejoining the extra fields into `Resolution Notes` recovers them.
  - The "too few fields" rows (2011, 2012, 2023) will be kept in `raw` and flagged, not typed.
- **`Case Number` works as the primary key.** No key appears in more than one year file. There are 0–51 duplicate keys within a file, and the upsert resolves them.
  - Case-number formats changed over the years: `NNNNNNNNNN-NNNNNNNNNNNN` in 2011, `NNNNNNNN-NNNNNNNNNNNN` or a 12-digit number in 2012–2021, and a 10-digit number from 2021. They'll be stored as text.
- **`none` is a null marker.** It fills `Latest Case Notes` on nearly every row before 2021, and `TaxID` on 7–88% of rows depending on the year.

### Coordinates
- **EPSG:2278 is confirmed.** Where both sources are valid, transformed X/Y and source lat/lon agree to a median of 4 m and a p99 of 7 m for 2011–2021. That matches lat/lon being rounded to 4 decimals.
- **The lat/lon vs X/Y disagreements (>100 m) are a placeholder, not noise.** Most disagreeing rows have lat/lon at **−95.3698, 29.7584, which is Houston City Hall**, while the X/Y is spread across the city.
  - Counts: about 7,300 rows in 2021 and 4,600–6,300 per year in 2023–24.
  - Phase 3 will trust X/Y over lat/lon and treat the City Hall point as missing. If kept, it would have become a fake downtown flood hotspot, since 70–120 flood-type records per year sit on it.
  - A few other heavily repeated points (a few hundred hits each on unrelated case types) look like office or facility placeholders. They will be blacklisted by a frequency rule.
- **2011 is the only year with a large coordinate gap:** 150,622 records (85%) have zero lat/lon **and** a blank address **and** no TaxID. Nothing can geocode them.
  - The 2011 file does cover all of 2011, but the city clearly didn't carry locations across from the old system for most of that year.

### Geocoding scope is much smaller than the brief assumed
- **Records needing geocoding, all years:** 184,686 (3.3%). 150,622 of those are the unrecoverable 2011 blanks, and the other ≈34,000 are spread over 2012–2026.
- **Almost all of the rest also have blank addresses.** In 2017, 5,178 of 5,354 are blank. Harvey is the only file with intersection-style addresses (`Intersection A&B`), and it has only 56.
- **For the flood/drainage types likely to be included,** about 1,300 records from 2012–2026 lack a point, plus 2,852 from 2011. That is 4,143 of ≈299,000, or **1.4%**.
- **The 95% target is therefore met from source coordinates alone.** HCAD address matching, intersection resolution and the Census batch geocoder each have only a few hundred candidates.
- **Recommendation:** implement Phase 3 as specified, but expect it to finish in minutes, not hours. A long-running background geocoder isn't needed.

### Free text
- **`Title` begins with the case-type name** (e.g. `Flooding - <case#> - <address>`), so a keyword like `flood` matches every `Flooding` record by name alone. Keyword rules will search `Description` and the notes fields, and strip the type prefix from `Title`.
- **Coverage varies by year:**

  | Years | `Description` populated | Notes populated |
  |---|---|---|
  | 2011 | 13% | 0% |
  | 2012–2020 | 60–78% | 0% |
  | 2021 | 81% | 38% |
  | 2022–2026 | 77–95% | 73–99% |

  Keyword rules will therefore catch far more in recent years. Case type is the only signal that's consistent across all years.

### Case types relevant to flooding (for your decision)
**Proposed tiers** (edit freely):

| Tier | Types | Records |
|---|---|---:|
| **strong** (case type alone) | `Flooding` (all years), `Low Area or Standing Water ROW` (2011–14) | 28,055, 4,845 |
| **moderate** | `Drainage`; `Inlet Clogged Needs Cleaning ROW`; `Street Drainage Restoration`; `Clean Storm Sewer`; `Storm Sewer Repair`/`Inspection`; the 2011–14 ditch types (`Roadside Ditch Maintenance`, `Regrade Roadside Ditch`, `Clean Off Road Ditch`, `Flush Roadside Ditch or Culvert`, `Regrade Off Road Ditch`, `Install Culvert`) | ≈131,000 |
| **via keywords only** | `Street Hazard`, `Street Condition`, `Pothole`, `Nuisance On Property`, `Drainage System Violation`, and any other type whose description matches a strong keyword | – |
| **excluded** | Everything in the brief's exclusion list, plus: `Floodplain` (a permitting inquiry), `Water/Sewer/Drainage Billing`, `Storm Debris Collection`, `Poor Drainage` (Parks facilities, not streets), `Storm Damage` (Harvey evacuation), the Sewer and Manhole types, and `Graffiti - Harris County Flood Control Property` | – |

- **Case types changed in 2015.** The 2011–14 types were detailed (e.g. `Inlet Clogged Needs Cleaning ROW`). From 2015 they collapse into `Drainage` and `Flooding`.
- **`Sewer Wastewater` does spike on storm days:** its daily count correlates 0.49 with the drainage/flooding count, against a median of 51/day. It stays excluded, as the brief requires.

### Storm days: known events are clearly visible
Daily drainage+flooding requests have a median of 18/day, a p95 of 69, a p99 of 175 and a p99.5 of 281.

| Event | Peak day(s) | Requests |
|---|---|---:|
| Memorial Day 2015 | May 25 / 26 / 27 | 179 / 245 / 419 |
| Tax Day 2016 | Apr 18 / 19 | 469 / 316 |
| Harvey | Aug 26 / 27 / 28, 2017 | 937 / 1,164 / 419 |
| Imelda | Sep 19, 2019 | 330 |
| June 2026 | Jun 5, Jun 15 | 193, 248 |
| Halloween 2015 | Oct 31 | 368 |
| Jan 18, 2017 | | 356 |
| Beryl | Jul 7, 2024 | **only 67**: the power outage suppressed reporting |

**One problem:** two weeks after Harvey (2017-09-05 to 09-21) there are 10 days of 400–1,600 `Flooding` requests with little rain, probably backlogged damage reports. A plain p99 threshold would call them storm days, and they would inflate the recurrence factor. Proposed fixes:
1. Merge consecutive storm days into **events**, so the recurrence factor counts distinct events rather than days. Harvey then counts once instead of about 20 times.
2. Add an editable exclusion/merge list in `risk/config.yaml`. It would ship with 2017-09-01 to 09-30 merged into the Harvey event.
