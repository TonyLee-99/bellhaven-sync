"""Link website locations to CRM accounts.

Address is the primary key for a facility (names change on every acquisition/rebrand; addresses
don't). A CRM account matches a location by address when it is in the same state, has the same
house number, the same street type and directional (Road != Drive), a near-identical street name,
and agrees on zip or city.

When the CRM street can't be compared - a PO Box, a blank street, or a house number that doesn't
match - a weaker fallback applies: same state and city, a strongly similar name, AND corroboration
(same zip or same phone). Fallback matches are always medium confidence. A name alone never matches.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .normalize import digits, is_po_box, norm_name, norm_street, zip5
from .website import Location

_STREET_TYPES = {"st", "rd", "ave", "blvd", "dr", "ln", "ct", "pl", "pkwy", "pike", "hwy", "cir", "ter", "sq", "way", "trl"}
_DIRECTIONALS = {"n", "s", "e", "w", "nw", "ne", "sw", "se"}


@dataclass
class Candidate:
    account: dict
    method: str                    # "address" | "fallback"
    score: int
    signals: dict = field(default_factory=dict)


_UNIT_WORDS = {"suite", "ste", "unit", "apt", "apartment", "bldg", "building", "fl", "floor", "rm", "room"}


def street_parts(s: str | None) -> dict:
    toks = norm_street((s or "").replace("#", " suite "))
    toks = toks.split()
    unit = ""
    for i, tok in enumerate(toks):                 # split off unit designators: "2301 Cleveland Rd Suite 1"
        if i > 0 and tok in _UNIT_WORDS:
            unit = " ".join(toks[i + 1:])
            toks = toks[:i]
            break
    num = toks[0] if toks and toks[0].isdigit() else ""
    rest = toks[1:] if num else toks
    dirs = [t for t in rest if t in _DIRECTIONALS]
    types = [t for t in rest if t in _STREET_TYPES]
    name = [t for t in rest if t not in _DIRECTIONALS and t not in _STREET_TYPES]
    return {"num": num, "dirs": dirs, "types": types, "name": " ".join(name), "unit": unit}


def _valid_pointer(acc: dict, field_name: str, by_id: dict | None) -> bool:
    target = acc.get(field_name) or ""
    if not target or target == acc.get("account_id"):
        return False
    if by_id is None:
        return True
    t = by_id.get(target)
    if t is None:
        return False
    if field_name == "duplicate_of_account":
        seen, cur = {acc.get("account_id")}, t
        while cur is not None and cur.get("duplicate_of_account"):        # follow the chain; any cycle = invalid
            if cur["account_id"] in seen:
                return False
            seen.add(cur["account_id"])
            cur = by_id.get(cur["duplicate_of_account"])
            if cur is None:
                return False
            if cur["account_id"] in seen:
                return False
        return True
    # a CHOW link is only "resolved" while its target is a live current record
    return t.get("status") != "Inactive" and not t.get("duplicate_of_account") and not t.get("chow_current_account")


def is_superseded(acc: dict, by_id: dict | None = None) -> bool:
    """Resolved by an earlier decision: a duplicate pointing at a real survivor, or a CHOW-linked old record
    pointing at a live current account. Self-links, dangling ids and dead CHOW targets don't count."""
    return _valid_pointer(acc, "duplicate_of_account", by_id) or _valid_pointer(acc, "chow_current_account", by_id)


def broken_pointers(acc: dict, by_id: dict) -> dict:
    """Link fields that are set but don't resolve (dangling / self / dead target) - surfaced for repair."""
    out = {}
    for f in ("duplicate_of_account", "chow_current_account"):
        if acc.get(f) and not _valid_pointer(acc, f, by_id):
            out[f] = acc[f]
    return out


def address_signals(loc: Location, acc: dict, street_min: int) -> dict | None:
    if (acc.get("billing_state") or "").upper() != loc.state.upper():
        return None
    a_street = acc.get("billing_street") or ""
    if is_po_box(a_street) or not a_street.strip():
        return None
    lp, ap = street_parts(loc.street), street_parts(a_street)
    if not lp["num"] or lp["num"] != ap["num"]:
        return None
    # a different street type or directional is a different street (Cleveland Rd vs Cleveland Dr)
    if lp["types"] and ap["types"] and lp["types"] != ap["types"]:
        return None
    if lp["dirs"] and ap["dirs"] and lp["dirs"] != ap["dirs"]:
        return None
    if lp["unit"] and ap["unit"] and lp["unit"] != ap["unit"]:      # Suite 1 vs Suite 2: different tenants
        return None
    name_score = fuzz.ratio(lp["name"], ap["name"])
    if name_score < street_min:
        return None
    zip_ok = zip5(acc.get("billing_zip")) == zip5(loc.zip)
    city_ok = (acc.get("billing_city") or "").strip().lower() == loc.city.strip().lower()
    if not (zip_ok or city_ok):
        return None
    return {
        "street_exact": norm_street(loc.street) == norm_street(a_street),
        "street_score": int(name_score),
        "zip_match": zip_ok,
        "city_match": city_ok,
        "name_score": int(fuzz.token_set_ratio(norm_name(loc.name), norm_name(acc.get("name")))),
        "phone_match": bool(digits(loc.phone)) and digits(loc.phone) == digits(acc.get("phone")),
    }


def fallback_signals(loc: Location, acc: dict, name_min: int) -> dict | None:
    """Name + city + state, corroborated by zip or phone. For PO Box / blank / mistyped streets."""
    if (acc.get("billing_state") or "").upper() != loc.state.upper():
        return None
    if (acc.get("billing_city") or "").strip().lower() != loc.city.strip().lower():
        return None
    name_score = fuzz.token_set_ratio(norm_name(loc.name), norm_name(acc.get("name")))
    if name_score < name_min:
        return None
    zip_ok = zip5(acc.get("billing_zip")) == zip5(loc.zip)
    phone_ok = bool(digits(loc.phone)) and digits(loc.phone) == digits(acc.get("phone"))
    if not (zip_ok or phone_ok):
        return None
    street = acc.get("billing_street") or ""
    lu, au = street_parts(loc.street)["unit"], street_parts(street)["unit"]
    if lu and au and lu != au:                                   # explicitly different suites: never the same facility
        return None
    if is_po_box(street):
        reason = "po_box"
    elif not street.strip():
        reason = "blank_street"
    else:
        # a typo, not a different street: same house number with a variant street, or the same street with
        # a mistyped house number. A completely different street is a different (or ambiguous) facility.
        lp, ap = street_parts(loc.street), street_parts(street)
        same_num = bool(lp["num"]) and lp["num"] == ap["num"]
        name_sim = fuzz.ratio(lp["name"], ap["name"])
        if not ((same_num and name_sim >= 70) or (not same_num and name_sim >= 90)):
            return None
        reason = "street_variant" if same_num else "house_number_typo"
    return {"street_exact": False, "street_score": 0, "zip_match": zip_ok, "city_match": True,
            "name_score": int(name_score), "phone_match": phone_ok, "fallback_reason": reason,
            "crm_street": street}


def candidates_for(loc: Location, accounts: list[dict], cfg: dict, lenient: bool = False) -> list[Candidate]:
    """lenient=True is only used for quarantined (incomplete) pages, to know which accounts they hold:
    same state + house number + street name, ignoring the missing zip/city."""
    street_min = int(cfg["matching"]["street_fuzzy_min"])
    if lenient:
        out = []
        lp = street_parts(loc.street)
        for acc in accounts:
            ap = street_parts(acc.get("billing_street"))
            if (acc.get("billing_state") or "").upper() == loc.state.upper() and lp["num"] and lp["num"] == ap["num"] \
                    and fuzz.ratio(lp["name"], ap["name"]) >= street_min:
                out.append(Candidate(acc, "address", 0, {}))
            elif (loc.city and (acc.get("billing_city") or "").lower() == loc.city.lower()
                  and fuzz.token_set_ratio(norm_name(loc.name), norm_name(acc.get("name"))) >= int(cfg["matching"]["name_fuzzy_min"])):
                out.append(Candidate(acc, "fallback", 0, {}))
        return out
    name_min = int(cfg["matching"]["name_fuzzy_min"])
    by_address, fallback = [], []
    for acc in accounts:
        if acc.get("name", "").endswith("(Parent Account)"):
            continue
        sig = address_signals(loc, acc, street_min)
        if sig is not None:
            score = 60 + (15 if sig["street_exact"] else 0) + (10 if sig["zip_match"] else 0) \
                + (5 if sig["city_match"] else 0) + (10 if sig["phone_match"] else 0)
            by_address.append(Candidate(acc, "address", min(score, 100), sig))
            continue
        sig = fallback_signals(loc, acc, name_min)
        if sig is not None:
            score = 35 + (10 if sig["zip_match"] else 0) + (15 if sig["phone_match"] else 0)
            fallback.append(Candidate(acc, "fallback", score, sig))
    # fallback candidates only count when nothing matches by address (otherwise they are a different facility)
    out = by_address or fallback
    out.sort(key=lambda c: (-c.score, c.account["account_id"]))
    return out


def same_address_accounts(acc: dict, accounts: list[dict], street_min: int) -> list[dict]:
    """Other CRM accounts at the same physical address as `acc` (used for successor detection)."""
    if is_po_box(acc.get("billing_street")) or not (acc.get("billing_street") or "").strip():
        return []
    probe = Location(slug="", name=acc.get("name", ""), street=acc.get("billing_street", ""),
                     city=acc.get("billing_city", ""), state=acc.get("billing_state", ""),
                     zip=acc.get("billing_zip", ""), care_offerings=[], url="")
    out = []
    for other in accounts:
        if other["account_id"] == acc["account_id"] or other.get("name", "").endswith("(Parent Account)"):
            continue
        if address_signals(probe, other, street_min) is not None:
            out.append(other)
    return out
