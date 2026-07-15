"""Shared pytest setup: make the bundled scripts importable.

The pipeline's deterministic pieces live in
.claude/skills/apartmentops/scripts/ so the skills can ship them; tests
import them as plain modules via this path hook.
"""

from __future__ import annotations

import pathlib
import sys

SCRIPTS = (
    pathlib.Path(__file__).resolve().parent.parent
    / ".claude" / "skills" / "apartmentops" / "scripts"
)
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
