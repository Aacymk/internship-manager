"""Company job boards, queried through their own public JSON APIs.

Greenhouse, Lever, Ashby, and Workday all expose the board behind a
company's careers page as unauthenticated JSON. No keys, no browser, no
scraping of rendered HTML — this is a company telling you directly what it
is hiring for, and it is the highest-trust signal in the system.

WORKDAY is the one that needs explaining, because most large employers use
it and its API is not obvious:

    POST /wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"}

`searchText` behaves badly in BOTH directions, which is why the query here
is broad and the filtering happens locally:

- Too narrow a query silently misses roles. "software intern 2027" only
  matches postings containing all three terms, so an internship whose title
  is just "Software Engineering Intern" never comes back.
- Too broad a query over-matches, because it is substring-based rather than
  word-based. "intern" matches "internal" and "international", which is how
  a search for internships returns "Vice President, Global Partner
  Marketing". `looks_like_internship` is what rejects those.

Location is the other Workday subtlety. The list endpoint reports
`locationsText`, which degrades to "2 Locations" when a posting spans
several — useless for filtering. The DETAIL endpoint returns a structured
`country` object, which is reliable. That distinction is what separates a
genuine US opening from the same job req posted only in another region.
"""
from __future__ import annotations

import abc
import html
import re
from datetime import datetime

import requests

from ..model import Listing, looks_like_internship
from .base import HEADERS, Source, TIMEOUT, get_json

MAX_WORKDAY_PAGES = 5
WORKDAY_PAGE_SIZE = 20


class JobBoard(Source):
    """A company's own board. Subclasses implement `fetch_postings`.

    Unlike a curated internship list — which contains nothing BUT internships
    and is therefore passed through untouched — a job board carries every
    role the company is hiring for. `looks_like_internship` is the one gate
    applied here, and it only asks whether a posting is an internship at all,
    never whether it is relevant. See radar/model.py for why that line is
    drawn where it is.
    """

    @abc.abstractmethod
    def fetch_postings(self) -> list[Listing]:
        """Every posting the board returns, unfiltered."""

    def fetch(self) -> list[Listing]:
        return [l for l in self.fetch_postings()
                if looks_like_internship(l.role)]


def _strip_html(s: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(s or "")).split())


def _iso_date(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


class GreenhouseBoard(JobBoard):
    kind = "greenhouse"

    def __init__(self, company: str, slug: str):
        self.company, self.slug = company, slug
        self.name = f"{company} (greenhouse)"

    def fetch_postings(self) -> list[Listing]:
        data = get_json("https://boards-api.greenhouse.io/v1/boards/"
                        f"{self.slug}/jobs")
        return [Listing(
            source=self.name, company=self.company, role=j.get("title", ""),
            url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", ""),
            posted=_iso_date(j.get("first_published")),
        ) for j in data.get("jobs", [])]


class LeverBoard(JobBoard):
    kind = "lever"

    def __init__(self, company: str, slug: str):
        self.company, self.slug = company, slug
        self.name = f"{company} (lever)"

    def fetch_postings(self) -> list[Listing]:
        data = get_json(f"https://api.lever.co/v0/postings/{self.slug}"
                        "?mode=json")
        out = []
        for p in data:
            posted = None
            if p.get("createdAt"):
                posted = datetime.fromtimestamp(p["createdAt"] / 1000).date()
            out.append(Listing(
                source=self.name, company=self.company,
                role=p.get("text", ""), url=p.get("hostedUrl", ""),
                location=(p.get("categories") or {}).get("location", ""),
                posted=posted,
            ))
        return out


class AshbyBoard(JobBoard):
    kind = "ashby"

    def __init__(self, company: str, slug: str):
        self.company, self.slug = company, slug
        self.name = f"{company} (ashby)"

    def fetch_postings(self) -> list[Listing]:
        data = get_json("https://api.ashbyhq.com/posting-api/job-board/"
                        f"{self.slug}")
        return [Listing(
            source=self.name, company=self.company, role=j.get("title", ""),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            location=j.get("location", ""),
            posted=_iso_date(j.get("publishedAt")),
        ) for j in data.get("jobs", [])]


class WorkdayBoard(JobBoard):
    """Workday. `board_id` is "tenant:N:site", read off the careers URL:

        https://salesforce.wd12.myworkdayjobs.com/External_Career_Site
                 tenant^     ^N   site^

    so the board_id is "salesforce:12:External_Career_Site".
    """
    kind = "workday"

    def __init__(self, company: str, board_id: str, search: str = "intern"):
        self.company = company
        try:
            tenant, n, site = str(board_id).split(":")
        except ValueError:
            raise ValueError(
                f'{company}: workday board_id should look like '
                f'"tenant:12:Site_Name", got "{board_id}"')
        self.tenant, self.n, self.site = tenant, n, site
        self.search = search
        self.base = f"https://{tenant}.wd{n}.myworkdayjobs.com"
        self.name = f"{company} (workday)"

    def _post(self, offset: int) -> dict:
        r = requests.post(
            f"{self.base}/wday/cxs/{self.tenant}/{self.site}/jobs",
            json={"appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE,
                  "offset": offset, "searchText": self.search},
            headers={**HEADERS, "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def fetch_postings(self) -> list[Listing]:
        out: list[Listing] = []
        for page in range(MAX_WORKDAY_PAGES):
            data = self._post(page * WORKDAY_PAGE_SIZE)
            postings = data.get("jobPostings") or []
            for p in postings:
                path = p.get("externalPath", "")
                out.append(Listing(
                    source=self.name, company=self.company,
                    role=p.get("title", ""),
                    url=f"{self.base}/en-US/{self.site}{path}",
                    location=p.get("locationsText", ""),
                    extra={"detail_path": path},
                ))
            if len(postings) < WORKDAY_PAGE_SIZE:
                break  # last page
            if (page + 1) * WORKDAY_PAGE_SIZE >= (data.get("total") or 0):
                break
        return out

    def country_of(self, listing: Listing) -> str:
        """Structured country for one posting, or "" if unavailable.

        Only the detail endpoint has this; see the module docstring for why
        `locationsText` cannot be trusted for multi-location postings.
        """
        path = listing.extra.get("detail_path")
        if not path:
            return ""
        try:
            data = get_json(f"{self.base}/wday/cxs/{self.tenant}"
                            f"/{self.site}{path}")
        except (requests.RequestException, ValueError):
            return ""
        info = data.get("jobPostingInfo") or {}
        country = info.get("country") or {}
        return country.get("descriptor", "") or ""


_KINDS = {
    "greenhouse": GreenhouseBoard,
    "lever": LeverBoard,
    "ashby": AshbyBoard,
    "workday": WorkdayBoard,
}


def build(company_name: str, kind: str, board_id) -> Source | None:
    """Instantiate a board source, or None if the config is unusable."""
    cls = _KINDS.get((kind or "").lower())
    if not cls or not board_id:
        return None
    try:
        return cls(company_name, board_id)
    except ValueError as e:
        print(f"    {e}")
        return None


def from_config(companies) -> list[Source]:
    out = []
    for c in companies:
        if not c.board:
            continue
        src = build(c.name, c.board[0], c.board[1])
        if src:
            out.append(src)
    return out
