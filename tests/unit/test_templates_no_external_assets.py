"""Every asset a template pulls in must come from this origin.

The chatroom UI is read from closed networks whose egress goes through an
allowlisting proxy. A page that sources an asset from a public CDN still
returns 200 -- the HTML is ours -- so the failure shows up only as content
that never arrives: HTMX is what issues the hx-get behind the thread and
event lists, and a blocked HTMX leaves the shell rendered and empty. That
is a slow thing to diagnose from the far side of the proxy, so the
templates are checked here instead.

The rule is about the *origin*, not the host: any absolute or
protocol-relative URL in a src= or href= is a request the browser sends
somewhere we do not control. Relative paths are fine -- they resolve
against whatever host served the page, which matters twice here, because
these pages are also served through Magickit's /ui proxy under a
different host than Conclair's own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from spirrow_conclair.main import STATIC_DIR, TEMPLATES_DIR

# src="..." / href='...' -- the attribute value, quotes stripped.
ASSET_REF = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)

# "https://unpkg.com/...", "//unpkg.com/..." -- anything carrying an origin.
EXTERNAL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:)?//")


def _templates() -> list[Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def test_templates_are_found() -> None:
    """Guard the guard: a moved template dir must not silently pass."""
    assert _templates(), f"no templates under {TEMPLATES_DIR}"


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_external_asset_origins(template: Path) -> None:
    text = template.read_text(encoding="utf-8")
    external = [
        (n, url)
        for n, line in enumerate(text.splitlines(), start=1)
        for url in ASSET_REF.findall(line)
        if EXTERNAL.match(url)
    ]
    assert not external, (
        f"{template.relative_to(TEMPLATES_DIR)} references an external origin: "
        + ", ".join(f"line {n}: {url}" for n, url in external)
        + " -- vendor the asset under src/spirrow_conclair/static/ and reference it by path."
    )


def test_the_check_catches_a_cdn_reference() -> None:
    """The regexes, against the line this test file exists because of."""
    line = '<script src="https://unpkg.com/htmx.org@1.9.10"></script>'
    assert [u for u in ASSET_REF.findall(line) if EXTERNAL.match(u)]


def test_relative_references_pass() -> None:
    for line in (
        '<script src="/static/js/htmx.min.js"></script>',
        '<link rel="stylesheet" href="/static/css/conclair.css">',
        '<a href="/ui/projects/x/threads">threads</a>',
    ):
        assert not [u for u in ASSET_REF.findall(line) if EXTERNAL.match(u)]


def test_htmx_is_vendored() -> None:
    """The self-hosted file the templates now point at actually exists."""
    htmx = STATIC_DIR / "js" / "htmx.min.js"
    assert htmx.is_file(), f"{htmx} is missing -- base.html would 404 on it"
    assert htmx.stat().st_size > 0
