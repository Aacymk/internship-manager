"""The contract every source implements.

A source is anything that can produce Listings: a curated job list, a
company's ATS, an RSS feed. Keeping them behind one small interface is what
lets the runner treat "5 GitHub lists and 30 job boards" as one stream, and
it is the seam a future user-defined source plugs into.

Sources MUST NOT raise. A single unreachable list must never take down a
run, so `collect()` traps exceptions and reports them as warnings — a
partial result is always more useful than a crash.
"""
from __future__ import annotations

import abc

import requests

from ..model import Listing

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = 20


class Source(abc.ABC):
    """Produces Listings. Subclasses implement `fetch`."""

    #: shown in reports so a noisy source can be identified and disabled
    name: str = "source"

    @abc.abstractmethod
    def fetch(self) -> list[Listing]:
        """Return every listing this source currently advertises.

        Filtering by season or role is deliberately NOT done here. Sources
        report what they see; the runner decides what is worth alerting on.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


def get_json(url: str, **kwargs):
    r = requests.get(url, headers={**HEADERS, "Accept": "application/json"},
                     timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r.json()


def get_text(url: str, **kwargs) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r.text


def collect(sources: list[Source], log=print) -> tuple[list[Listing], list[str]]:
    """Run every source, tolerating failures. Returns (listings, warnings).

    Duplicates are collapsed by canonical URL, because the same posting
    routinely appears on several lists with different tracking params.
    """
    listings: dict[str, Listing] = {}
    warnings: list[str] = []
    for src in sources:
        try:
            found = src.fetch()
        except Exception as e:  # noqa: BLE001 - see module docstring
            msg = f"{src.name}: unavailable ({type(e).__name__}: {e})"
            warnings.append(msg)
            log(f"    {msg} - skipped")
            continue
        new = 0
        for listing in found:
            if not listing.url:
                continue
            if listing.key not in listings:
                listings[listing.key] = listing
                new += 1
        log(f"    {src.name}: {len(found)} listing(s), {new} unique")
    return list(listings.values()), warnings
