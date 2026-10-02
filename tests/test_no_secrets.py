"""Guard against committing secrets: real ICS tokens, URLs or coordinates.

Spec §15: real ICS links, tokens, school names, addresses and coordinates must
never enter git. This test scans every git-tracked file and fails on a match.

The patterns themselves live in this file, so this file and the plan documents
that quote the example SchoolSoft URL are allowlisted. Fixtures must stay
sanitised, so they are *not* allowlisted.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from sl_coords import PUBLIC_COORD_STRINGS

REPO_ROOT = Path(__file__).resolve().parents[1]

# A SchoolSoft/Vklass ical-feed token path, e.g. the one shared in chat.
ICS_TOKEN_RE = re.compile(r"ical-feed/parent/[A-Za-z0-9_-]{20,}")

# A Vklass-style token query parameter.
VKLASS_TOKEN_RE = re.compile(r"vklass\.se/.*(?:token|key)=[A-Za-z0-9_-]{16,}", re.I)

# A high-precision Nordic latitude (5x.ddddd+), the kind a real home/school
# address would produce. Sanitised fixtures use placeholders or integers.
COORD_RE = re.compile(r"\b5[5-9]\.\d{4,}\b")

# A tight allowlist of known-public reference coordinates (central-Stockholm
# stations used by the SL tests/fixtures, established as public in the T00
# investigation). These exact values are permitted; any other Nordic-looking
# coordinate still fails. The single source of truth lives in tests/sl_coords.py.
PUBLIC_COORDS = PUBLIC_COORD_STRINGS

PATTERNS = {
    "ICS feed token": ICS_TOKEN_RE,
    "Vklass token URL": VKLASS_TOKEN_RE,
    "real coordinate": COORD_RE,
}

# Files allowed to contain the literal patterns (this scanner, and plan docs
# that reference the example URL that was shared in chat). Paths are relative to
# the repo root.
ALLOWLIST = {
    "tests/test_no_secrets.py",
    "home-assistant-avgangsplan.md",
    "docs/implementation-plan.md",
    "docs/implementation-plan.json",
}


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_no_secrets_in_tracked_files() -> None:
    findings: list[str] = []
    for rel_path in _tracked_files():
        if rel_path in ALLOWLIST:
            continue
        path = REPO_ROOT / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                # Permit the explicitly-named public reference coordinates; any
                # other Nordic-looking coordinate is still a finding.
                if pattern is COORD_RE and value in PUBLIC_COORDS:
                    continue
                findings.append(f"{rel_path}: {label} ({value!r})")
                break

    assert not findings, "Possible secrets found in tracked files:\n" + "\n".join(
        findings
    )


def test_scanner_detects_a_planted_token(tmp_path: Path) -> None:
    """Sanity check: the scanner would catch a real token path."""
    sample = "https://sms.schoolsoft.se/x/rest-api/ical-feed/parent/" + "A" * 40
    assert ICS_TOKEN_RE.search(sample) is not None


def test_coord_allowlist_is_tight() -> None:
    """Only the named public coords are exempt; other Nordic coords still trip."""
    # A non-allowlisted high-precision Nordic latitude is still a match.
    assert COORD_RE.search("59.31234") is not None
    assert "59.31234" not in PUBLIC_COORDS
    # The named public reference coordinates are both allowlisted.
    assert PUBLIC_COORDS <= {"59.33258", "59.34300"}
    for value in PUBLIC_COORDS:
        assert COORD_RE.fullmatch(value) is not None
