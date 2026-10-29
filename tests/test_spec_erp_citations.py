"""Every ERP decision the agent's spec cites is one of the agent feed's own.

The ERP renumbered its DECISIONS entries when ``agent-feed`` merged with
``main`` (2026-10-29): the agent feed's #1455-#1477 became #1545-#1567,
because main's own rounds had taken #1455-#1544 first. The spec kept the old
numbers in four files, so every citation pointed at an unrelated decision of
main's — a Sakkiz band round, the Uch band, the AI sotuvchi (the merge review).

The fence: in ``docs/spec``, a DECISIONS citation from #1400 up must be in the
agent feed's block. Lower numbers are main's long-settled decisions (#1223,
#1381 …), which no merge renumbers. When the ERP adds an agent-feed decision,
``AGENT_FEED_DECISIONS`` grows with it — in the same commit that cites it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "spec"

#: The ERP's agent-feed decisions (fullprompt DECISIONS.md, the section «The
#: client-finding agent's door and its two feeds»), after the merge with main.
AGENT_FEED_DECISIONS = range(1545, 1571)
#: From here up a citation is recent enough that a merge could have moved it.
RENUMBERED_FROM = 1400

CITATION = re.compile(r"#(1[0-9]{3})(?![0-9])")


def citations(text: str) -> list[int]:
    return [int(match.group(1)) for match in CITATION.finditer(text)]


def test_the_pattern_reads_ranges_and_lists():
    found = citations("DECISIONS #1545–#1549, #1563 and (#1567).")
    assert found == [1545, 1549, 1563, 1567]
    # A five-digit run is no citation of a four-digit entry.
    assert citations("#15450") == []


def test_every_recent_erp_citation_is_the_agent_feeds():
    stray: list[str] = []
    for path in sorted(SPEC.glob("*.md")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for number in citations(line):
                if number >= RENUMBERED_FROM and number not in AGENT_FEED_DECISIONS:
                    stray.append(f"{path.relative_to(ROOT)}:{lineno}: #{number}")
    header = "a citation outside the agent feed's DECISIONS block:\n"
    assert stray == [], header + "\n".join(stray)


def test_the_spec_still_cites_the_block_at_all():
    # A fence over nothing proves nothing: the citations it guards exist.
    texts = [path.read_text(encoding="utf-8") for path in SPEC.glob("*.md")]
    found = [n for text in texts for n in citations(text)]
    assert any(n in AGENT_FEED_DECISIONS for n in found)
