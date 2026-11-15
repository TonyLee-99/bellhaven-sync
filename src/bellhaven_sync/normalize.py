"""Normalization helpers used for matching (never written back to the CRM)."""
from __future__ import annotations

import re

_SUFFIX = {
    "street": "st", "str": "st", "road": "rd", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy", "pk": "pike",
    "pke": "pike", "highway": "hwy", "circle": "cir", "terrace": "ter", "square": "sq",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
_PO_BOX = re.compile(r"^\s*p\.?\s*o\.?\s*box\b", re.I)


def norm_text(s: str | None) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def norm_street(s: str | None) -> str:
    return " ".join(_SUFFIX.get(w, w) for w in norm_text(s).split())


def house_number(s: str | None) -> str:
    m = re.match(r"\s*(\d+)", s or "")
    return m.group(1) if m else ""


def is_po_box(s: str | None) -> bool:
    return bool(_PO_BOX.match(s or ""))


def zip5(s: str | None) -> str:
    m = re.match(r"\s*(\d{5})", s or "")
    return m.group(1) if m else ""


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def norm_name(s: str | None) -> str:
    """Name comparison form: lowercase, '&'->and, common abbreviations expanded."""
    t = norm_text(s)
    t = re.sub(r"\brehab\b", "rehabilitation", t)
    t = re.sub(r"\bcentre\b", "center", t)
    t = re.sub(r"\bhealth care\b", "healthcare", t)
    return t


MONEY_FIELDS = {"lifetime_revenue", "outstanding_ar"}


def canon(field: str, value):
    """One comparison form shared by planner guards, the stale check and read-back verification, so that
    harmless representation differences (None vs "", 47000 vs "47000.00", ZIP+4, stray whitespace)
    never look like a change."""
    if value is None:
        return 0.0 if field in MONEY_FIELDS else ""
    if field in MONEY_FIELDS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if field == "billing_zip":
        return zip5(str(value)) or str(value).strip()
    if isinstance(value, str):
        return value.strip()
    return value


def same(field: str, a, b) -> bool:
    return canon(field, a) == canon(field, b)
