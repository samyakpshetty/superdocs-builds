"""The template binding engine.

A report format is a real HTML document with two kinds of marker in it:

* ``{{placeholder}}`` — a single value.
* ``<!-- region:name -->`` … ``<!-- /region:name -->`` — a block repeated once per item, and
  regions nest: a *system* contains *findings*, and a finding contains *photos*.

Binding is deliberately strict in both directions. A template that asks for a region this
build does not supply is an error naming the region and the fix; a placeholder left unfilled
is an error rather than an empty gap in a document a buyer will read. The one thing that is
never an error is a region with nothing to put in it — a system with no findings is a real
and important outcome, and it renders as such.

Structure never passes through a model. Which system a finding appears under, and in what
order, is decided here and nowhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REGION = re.compile(
    r"<!--\s*region:(?P<name>[a-z_]+)\s*-->(?P<body>.*?)<!--\s*/region:(?P=name)\s*-->",
    re.DOTALL,
)
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


class TemplateError(Exception):
    """A template could not be bound. The message names the cause and the fix."""


@dataclass(frozen=True)
class Region:
    """One repeatable block, with whatever regions it contains."""

    name: str
    body: str

    def children(self) -> dict[str, Region]:
        return {
            m.group("name"): Region(m.group("name"), m.group("body"))
            for m in _REGION.finditer(self.body)
        }


def find_region(html: str, name: str) -> Region:
    for match in _REGION.finditer(html):
        if match.group("name") == name:
            return Region(name, match.group("body"))
    available = sorted({m.group("name") for m in _REGION.finditer(html)})
    raise TemplateError(
        f"this template has no <!-- region:{name} --> block. It defines {available or 'none'}. "
        f"Add the region, or choose a format that has one."
    )


def regions_in(html: str) -> set[str]:
    return {m.group("name") for m in _REGION.finditer(html)}


def declares_region(html: str, name: str) -> bool:
    """Whether a template declares a region anywhere, at any nesting depth.

    A format decides what it carries. The repair-priority sheet has no photo region on
    purpose, and the export verifier reads this rather than holding every format to the
    same promise.
    """
    return bool(re.search(rf"<!--\s*region:{re.escape(name)}\s*-->", html))


def placeholders_in(html: str) -> set[str]:
    """Every placeholder name, including those inside regions."""
    return set(_PLACEHOLDER.findall(html))


def fill(body: str, values: dict[str, str], *, where: str) -> str:
    """Substitute every placeholder in ``body``, refusing to leave one unfilled."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    out = _PLACEHOLDER.sub(replace, body)
    if missing:
        raise TemplateError(
            f"{where}: nothing to put in {sorted(set(missing))}. Either supply the value or "
            f"remove the placeholder from the template — a report must not go to a buyer "
            f"with a gap where a fact belongs."
        )
    return out


def render_region(region: Region, rows: list[dict[str, str]], *, where: str) -> str:
    """Repeat a region once per row. No rows renders nothing, which is a valid outcome."""
    return "".join(fill(region.body, row, where=f"{where}/{region.name}") for row in rows)


def strip_regions(html: str) -> str:
    """Remove every region block, leaving the surrounding document.

    Used to build the outer shell before the rendered regions are spliced back in.
    """
    return _REGION.sub("", html)


def replace_region(html: str, name: str, rendered: str) -> str:
    """Swap one region block for its rendered content, markers and all."""
    found = False

    def swap(match: re.Match[str]) -> str:
        nonlocal found
        if match.group("name") != name:
            return match.group(0)
        found = True
        return rendered

    out = _REGION.sub(swap, html, count=0)
    if not found:
        raise TemplateError(f"no region named {name!r} to replace")
    return out
