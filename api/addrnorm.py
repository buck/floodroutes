"""Street-name normalisation shared by the index builder and the API.

"North Braeswood Boulevard", "N. Braeswood Blvd" and "n braeswood blvd" all become
"N BRAESWOOD BLVD", so TIGER, OSM and what people type match each other.
"""
import re

WORDS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR", "ROAD": "RD",
    "LANE": "LN", "PARKWAY": "PKWY", "FREEWAY": "FWY", "HIGHWAY": "HWY", "EXPRESSWAY": "EXPY",
    "COURT": "CT", "CIRCLE": "CIR", "PLACE": "PL", "TRAIL": "TRL", "TERRACE": "TER", "SQUARE": "SQ",
    "CROSSING": "XING", "POINT": "PT", "HEIGHTS": "HTS", "ESTATES": "ESTS", "MOUNT": "MT",
    "SAINT": "ST", "FARM": "FM", "MARKET": "MKT", "ROUTE": "RTE", "LOOP": "LOOP",
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH",
}


def norm_street(s: str) -> str:
    s = re.sub(r"[.,#']", " ", (s or "").upper())
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(WORDS.get(w, w) for w in s.split(" "))
