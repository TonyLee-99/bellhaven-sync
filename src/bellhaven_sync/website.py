"""Scrape every Bellhaven location from the operator website.

Discovery is deliberately belt-and-braces: the paginated directory is followed via its "Next" links,
and the homepage / About page are also scanned for community links, because a newly acquired
community can be announced on the homepage before it is added to the directory.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import Settings, guard_remote

SLUG_RE = re.compile(r"^/communities/([a-z0-9-]+)/?$")
CITY_LINE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})(?:\s+(?P<zip>\d{5}(?:-\d{4})?))?$")


@dataclass
class Location:
    slug: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list[str]
    url: str
    administrator: str = ""      # evidence only
    phone: str = ""              # evidence only
    found_via: list[str] = field(default_factory=list)
    listed_in_directory: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Scraper:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.base = settings.website_base
        self.list_path = settings["website"]["list_path"]
        self.max_pages = int(settings["website"]["max_list_pages"])
        guard_remote(self.base)
        self.http = client or httpx.Client(timeout=20, follow_redirects=True,
                                           headers={"User-Agent": "bellhaven-sync/0.1"})

    def _get(self, path_or_url: str) -> BeautifulSoup:
        url = urljoin(self.base + "/", path_or_url.lstrip("/")) if not path_or_url.startswith("http") else path_or_url
        guard_remote(url)
        r = self.http.get(url)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")

    @staticmethod
    def _slugs(soup: BeautifulSoup) -> list[str]:
        out = []
        for a in soup.find_all("a", href=True):
            m = SLUG_RE.match(urlparse(a["href"]).path)
            if m and m.group(1) not in out:
                out.append(m.group(1))
        return out

    def discover(self) -> tuple[dict[str, dict], dict]:
        """Return {slug: directory card info} and crawl stats."""
        found: dict[str, dict] = {}
        stats = {"directory_pages": 0, "directory_claimed_total": None, "homepage_slugs": 0,
                 "directory_finished": False, "failed_sources": []}
        path, seen_pages = self.list_path, set()
        while path and len(seen_pages) < self.max_pages:
            try:
                soup = self._get(path)
            except httpx.HTTPError as e:
                stats["failed_sources"].append({"path": path, "error": str(e)[:160]})
                break
            page_no = parse_qs(urlparse(path).query).get("page", ["1"])[0]
            if page_no in seen_pages:
                break
            seen_pages.add(page_no)
            stats["directory_pages"] += 1
            m = re.search(r"(\d+)\s+communities listed", soup.get_text(" "))
            if m:
                stats["directory_claimed_total"] = int(m.group(1))
            for card in soup.select(".card"):
                a = card.find("a", href=True)
                sm = SLUG_RE.match(urlparse(a["href"]).path) if a else None
                if not sm:
                    continue
                city = card.select_one(".city")
                found.setdefault(sm.group(1), {
                    "found_via": ["directory"],
                    "card_name": a.get_text(strip=True),
                    "card_city": city.get_text(strip=True) if city else "",
                    "card_badges": [b.get_text(strip=True) for b in card.select(".badge")],
                })
            nxt = next((a["href"] for a in soup.find_all("a", href=True)
                        if "next" in a.get_text(strip=True).lower()), None)
            path = nxt
            if not nxt:
                stats["directory_finished"] = True
        for extra in ("/", "/about"):
            try:
                soup = self._get(extra)
            except httpx.HTTPError as e:
                stats["failed_sources"].append({"path": extra, "error": str(e)[:160]})
                continue
            slugs = self._slugs(soup)
            if extra == "/":
                stats["homepage_slugs"] = len(slugs)
                m = re.search(r"serve\s+(\d+)\s+communities", soup.get_text(" "))
                if m:
                    stats["homepage_claimed_total"] = int(m.group(1))
            for s in slugs:
                found.setdefault(s, {"found_via": []})["found_via"].append(f"page:{extra}")
        return found, stats

    def parse_detail(self, slug: str, card: dict) -> Location:
        url = f"{self.base}/communities/{slug}"
        soup = self._get(f"/communities/{slug}")
        name = soup.select_one("h1").get_text(strip=True)
        fields: dict[str, object] = {}
        for dt in soup.select("dl.detail dt"):
            dd = dt.find_next_sibling("dd")
            key = dt.get_text(strip=True).lower()
            if key == "address":
                lines = [s.strip() for s in dd.get_text("\n").split("\n") if s.strip()]
                fields["address_lines"] = lines
            elif key == "care offerings":
                fields["care"] = [b.get_text(strip=True) for b in dd.select(".badge")] or \
                                 [dd.get_text(strip=True)]
            else:
                fields[key] = dd.get_text(" ", strip=True)
        lines = fields.get("address_lines", [])
        warnings: list[str] = []
        street, city, state, zip_ = (lines[0] if lines else ""), "", "", ""
        m = CITY_LINE.match(lines[-1]) if len(lines) >= 2 else None
        if m:
            city, state, zip_ = m.group("city"), m.group("state"), m.group("zip") or ""
        else:
            warnings.append(f"could not parse address {lines!r}")
        loc = Location(slug=slug, name=name, street=street, city=city, state=state, zip=zip_,
                       care_offerings=list(fields.get("care", [])), url=url,
                       administrator=str(fields.get("administrator", "")),
                       phone=str(fields.get("phone", "")),
                       found_via=card.get("found_via", []),
                       listed_in_directory="directory" in card.get("found_via", []))
        # cross-check the directory card against the detail page
        if card.get("card_name") and card["card_name"] != name:
            warnings.append(f"directory name {card['card_name']!r} differs from page title")
        if card.get("card_city") and card["card_city"] != f"{city}, {state}":
            warnings.append(f"directory city {card['card_city']!r} differs from address")
        if card.get("card_badges") and sorted(card["card_badges"]) != sorted(loc.care_offerings):
            warnings.append("directory care offerings differ from detail page")
        if not loc.listed_in_directory:
            warnings.append("not listed in the community directory (found via " +
                            ", ".join(loc.found_via) + ")")
        loc.warnings = warnings
        return loc

    def scrape(self) -> tuple[list[Location], dict]:
        found, stats = self.discover()
        locs, failed = [], []
        for slug, card in sorted(found.items()):
            try:
                locs.append(self.parse_detail(slug, card))
            except (httpx.HTTPError, AttributeError, ValueError) as e:   # one bad page must not hide the rest
                failed.append({"slug": slug, "error": str(e)[:200]})
        stats["locations"] = len(locs)
        stats["failed_pages"] = failed
        stats["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return locs, stats
