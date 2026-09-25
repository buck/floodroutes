"""Parser for the city's pipe-delimited 311 extracts (both D365 and Harvey schemas).

- The header row is found by content (a line containing "case number" and many pipes),
  after a banner of variable length.
- A record starts on a line beginning with a case number, unless the record in
  progress is still short of the header's field count and appending the line
  would not overshoot it (then the line is an embedded line break in free text).
- Extra fields (stray pipes in free text) are rejoined into the last column.
"""
import re
from dataclasses import dataclass
from pathlib import Path

REC_START = re.compile(r"^\s*\d[\d-]*\s*\|")
SEPARATOR = re.compile(r"^[-|\s]+$")


def snake(h: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", h.strip()).strip("_").lower()
    return {"365_case_number": "case_number_365"}.get(s, s)


@dataclass
class Parsed:
    header: list[str]          # original header strings
    columns: list[str]         # snake_case column names
    banner: list[str]
    extract_banner: str | None  # timestamp text from the banner, if any


def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace")


def read(path: Path):
    """Return (Parsed, iterator of (line_number, fields, parse_status))."""
    lines = path.read_bytes().split(b"\n")
    banner, hdr_i = [], None
    for i, ln in enumerate(lines[:50]):
        t = _decode(ln.rstrip(b"\r"))
        if re.search("case number", t, re.I) and t.count("|") >= 10:
            hdr_i = i
            break
        banner.append(t)
    if hdr_i is None:
        raise ValueError(f"{path.name}: no header row found")
    header = [h.strip() for h in _decode(lines[hdr_i].rstrip(b"\r")).split("|")]
    nh = len(header)
    m = None
    for b in banner:
        m = m or re.search(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]", b)
    meta = Parsed(header, [snake(h) for h in header], banner, m.group(1) if m else None)

    def records():
        cur, cur_line, cur_nf = None, None, 0
        for i in range(hdr_i + 1, len(lines)):
            t = _decode(lines[i].rstrip(b"\r"))
            if not t.strip() or SEPARATOR.match(t):
                continue
            nf = t.count("|") + 1
            starts = REC_START.match(t) is not None
            if cur is not None and (not starts or (cur_nf < nh and cur_nf + nf - 1 <= nh)):
                cur += "\n" + t
                cur_nf += nf - 1
                continue
            if cur is not None:
                yield _finish(cur_line, cur, nh)
            if starts:
                cur, cur_line, cur_nf = t, i + 1, nf
            else:
                cur = None  # orphan line before any record
        if cur is not None:
            yield _finish(cur_line, cur, nh)

    return meta, records()


def _finish(line_no: int, text: str, nh: int):
    f = [v.strip() for v in text.split("|")]
    if len(f) == nh:
        return line_no, f, "ok"
    if len(f) > nh:
        return line_no, f[:nh - 1] + ["|".join(f[nh - 1:])], "extra_fields_joined"
    if len(f) == nh - 1:  # seen in 2011-2024: complete record without the trailing column
        return line_no, f + [None], "missing_last_field"
    return line_no, f + [None] * (nh - len(f)), "short"
