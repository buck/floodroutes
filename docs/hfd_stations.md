# HFD stations vs the 311 risk surface

**Source of the comments:** *Measuring Flooding with the Houston Fire Department*, S. Perry & R. Stein, Rice University / Kinder Institute, report to HFD (2019). Survey of 201 officers at all 88 stations, Sept–Oct 2018. Appendix A holds officer comments; the 1,383 surveyed flood locations are not published. Page: https://kinder.rice.edu/research/measuring-flooding-houston-fire-department

**Station locations:** OSM (`amenity=fire_station`, named "Houston Fire Department Station N"): 83 stations found. **Risk:** sum of 311 risk-hex scores within 300 m and 1 km of the station, and its percentile among all stations (100 = most 311 flood history around it).

| Station | Officers' report | Score ≤300 m | pctile | Score ≤1 km | pctile | Nearest risk area | High/medium areas ≤1 km |
|---:|---|---:|---:|---:|---:|---|---:|
| 5 | *cut off*: water twice in 10 years covered the lot; vehicles moved inside | 25 | 28 | 2052 | 92 | low at 240 m | 18 |
| 7 | *cut off*: Elgin at Austin floods in front of the station, deeper over 8 years | 489 | 99 | 1661 | 82 | low at 30 m | 12 |
| 16 | *cut off*: cannot leave to make runs: flooding right outside the station | 386 | 96 | 2889 | 96 | low at 0 m | 18 |
| 26 | *cut off*: station flooded 3-5 times; Dixie Dr under several feet | 57 | 53 | 533 | 43 | low at 236 m | 3 |
| 28 | *cut off*: ambulance and chief's car cannot leave after heavy rain (driveway slope) | 261 | 93 | 3577 | 99 | low at 0 m | 11 |
| 31 | *cut off*: Crosstimbers floods in front of the station; units locked in | 142 | 73 | 1692 | 83 | low at 2 m | 14 |
| 35 | *cut off*: flooding in front of the station delays response | 191 | 83 | 2789 | 95 | low at 19 m | 19 |
| 45 | *cut off*: station becomes an island; cannot get off McCarty Rd | 50 | 49 | 445 | 39 | low at 393 m | 1 |
| 52 | *cut off*: usually cannot leave the station because Hartsook floods | 65 | 58 | 378 | 34 | low at 88 m | 0 |
| 68 | *cut off*: flooded into the station by the adjacent bayou, last three years | 208 | 84 | 1027 | 69 | medium at 0 m | 6 |
| 72 | *cut off*: Bay Area Blvd at Saturn Ln floods to the station front | 9 | 16 | 54 | 8 | low at 1070 m | 0 |
| 83 | *cut off*: Richmond west of the Beltway floods, incl. Breezewood where the station is | 6 | 11 | 76 | 12 | low at 876 m | 0 |
| 39 | *harvey*: Harvey: all area around the station flooded, no access | 216 | 88 | 1776 | 88 | low at 12 m | 15 |
| 65 | *harvey*: Harvey: station isolated by street flooding | – | – | – | – | not in OSM | – |
| 69 | *harvey*: Harvey: could not cross Buffalo Bayou | 8 | 14 | 1009 | 67 | medium at 465 m | 9 |
| 3 | *dry*: no street-flooding issues around the station | 43 | 43 | 673 | 53 | low at 246 m | 3 |
| 50 | *dry*: Harvey: area around the station high and dry for 1,000 ft | 25 | 29 | 321 | 29 | medium at 541 m | 1 |

## Summary

- The 12 stations officers describe as cut off in ordinary heavy rain have a median 1 km risk percentile of **75** (50 = typical station).
- 6 of 12 are in the top quarter of stations by nearby 311 flood history; 5 are below the median (stations 26, 45, 52, 72, 83).
- Below-median stations are where 311 under-sees flooding that firefighters experience: a station's own access road (Hartsook at 52, McCarty at 45, Bay Area Blvd at 72, Dixie Dr at 26, Richmond at 83) can flood without many residents calling it in. Station 72 is in Clear Lake, where the model shows almost nothing.
- Caveats: the survey is from 2018 and station locations are today's OSM points (a few stations may have moved; station 65 is not in OSM). Station 83 has two conflicting comments. Twelve stations is a small sample.
- Station-access flooding is a routing problem in reverse: the question is not which street to avoid on the way somewhere, but whether a unit can leave at all. That needs live data (gauges, sensors, or HFD's own reports), not history alone.

## All stations (ranked by risk within 1 km)

| Station | Score ≤300 m | Score ≤1 km | Nearest risk area |
|---:|---:|---:|---|
| 11 | 1067 | 10153 | low at 0 m |
| 28 | 261 | 3577 | low at 0 m |
| 15 | 212 | 3023 | low at 44 m |
| 16 | 386 | 2889 | low at 0 m |
| 35 | 191 | 2789 | low at 19 m |
| 6 | 253 | 2444 | low at 3 m |
| 4 | 48 | 2089 | low at 204 m |
| 5 | 25 | 2052 | low at 240 m |
| 46 | 264 | 1900 | low at 168 m |
| 58 | 132 | 1843 | low at 102 m |
| 39 | 216 | 1776 | low at 12 m |
| 67 | 127 | 1769 | low at 138 m |
| 9 | 244 | 1737 | low at 73 m |
| 43 | 168 | 1694 | low at 59 m |
| 31 | 142 | 1692 | low at 2 m |
| 7 | 489 | 1661 | low at 30 m |
| 34 | 172 | 1566 | low at 120 m |
| 27 | 95 | 1558 | low at 1 m |
| 19 | 208 | 1479 | low at 22 m |
| 17 | 265 | 1302 | low at 0 m |
| 8 | 174 | 1258 | low at 11 m |
| 30 | 126 | 1237 | low at 39 m |
| 32 | 97 | 1126 | low at 64 m |
| 60 | 163 | 1081 | medium at 5 m |
| 56 | 78 | 1058 | medium at 264 m |
| 55 | 224 | 1044 | low at 0 m |
| 68 | 208 | 1027 | medium at 0 m |
| 69 | 8 | 1009 | medium at 465 m |
| 61 | 65 | 990 | low at 150 m |
| 49 | 485 | 977 | low at 131 m |
| 38 | 51 | 937 | low at 239 m |
| 23 | 18 | 835 | low at 336 m |
| 22 | 175 | 799 | low at 82 m |
| 66 | 45 | 792 | low at 338 m |
| 62 | 21 | 782 | low at 417 m |
| 33 | 43 | 776 | low at 83 m |
| 104 | 119 | 744 | medium at 252 m |
| 20 | 182 | 721 | high at 0 m |
| 12 | 105 | 704 | low at 151 m |
| 3 | 43 | 673 | low at 246 m |
| 77 | 38 | 606 | low at 290 m |
| 102 | 45 | 601 | low at 227 m |
| 29 | 105 | 594 | low at 68 m |
| 57 | 43 | 582 | low at 110 m |
| 75 | 38 | 558 | low at 244 m |
| 40 | 146 | 554 | medium at 37 m |
| 13 | 43 | 548 | low at 362 m |
| 26 | 57 | 533 | low at 236 m |
| 59 | 28 | 477 | low at 269 m |
| 37 | 27 | 473 | low at 698 m |
| 82 | 12 | 458 | low at 315 m |
| 45 | 50 | 445 | low at 393 m |
| 25 | 69 | 418 | low at 143 m |
| 2 | 54 | 411 | low at 98 m |
| 78 | 61 | 393 | low at 140 m |
| 52 | 65 | 378 | low at 88 m |
| 101 | 12 | 376 | low at 503 m |
| 44 | 27 | 341 | low at 437 m |
| 47 | 85 | 339 | medium at 256 m |
| 50 | 25 | 321 | medium at 541 m |
| 70 | 107 | 321 | low at 70 m |
| 103 | 59 | 320 | medium at 273 m |
| 41 | 26 | 312 | low at 729 m |
| 76 | 23 | 297 | medium at 789 m |
| 51 | 38 | 293 | low at 206 m |
| 21 | 2 | 249 | low at 747 m |
| 80 | 32 | 210 | low at 531 m |
| 73 | 48 | 178 | low at 54 m |
| 42 | 23 | 173 | low at 305 m |
| 93 | 1 | 119 | low at 411 m |
| 74 | 12 | 117 | low at 632 m |
| 86 | 12 | 108 | low at 1234 m |
| 81 | 0 | 77 | low at 970 m |
| 83 | 6 | 76 | low at 876 m |
| 71 | 9 | 74 | low at 1508 m |
| 10 | 7 | 54 | low at 1092 m |
| 72 | 9 | 54 | low at 1070 m |
| 96 | 3 | 33 | low at 1835 m |
| 90 | 3 | 29 | low at 1479 m |
| 63 | 4 | 17 | low at 1959 m |
| 92 | 7 | 13 | low at 2754 m |
| 54 | 0 | 1 | none within 3 km |
| 99 | 0 | 0 | none within 3 km |
