"""Program configuration: per-program sheet geometry for the sorting agent.

Mirrors the ai_fellowship_agent ProgramConfig pattern: one pipeline, multiple
programs, config-driven. The active program is selected at startup (CLI
``--program`` arg or the Streamlit sidebar selector) and resolves which
Google Sheet + tab to read from / write back to.

Only sheet geometry varies per program (sheet_id, sheet_range). Everything
else — column auto-detection, dedup pipeline, country routing — is shared
across programs and lives in app.py / pipeline.py unchanged.
"""
import os
from dataclasses import dataclass
from typing import Literal

ProgramName = Literal["r2b", "alchemist"]


@dataclass(frozen=True)
class SortingConfig:
    """Per-program sheet geometry for the sorting pipeline.

    Holds env-var NAMES (not resolved values) so instances can be created
    at import time. Resolution happens in Config.from_env / app.py at call
    time when the env is actually loaded.

    Fields:
        program:             Internal program key ('r2b' or 'alchemist').
        sheet_id_env:        Name of the env var holding the Google Sheet ID.
        sheet_range_env:     Name of the env var holding the tab name.
        program_name:        Human-friendly display name for the sidebar.
        default_sheet_range: Fallback tab name when the env var is unset.
    """

    program: ProgramName
    sheet_id_env: str
    sheet_range_env: str
    program_name: str
    default_sheet_range: str = "Form Responses 1"


# --- R2B ----------------------------------------------------------------

R2B_SORTING_CONFIG = SortingConfig(
    program="r2b",
    sheet_id_env="R2B_SHEET_ID",
    sheet_range_env="R2B_SHEET_RANGE",
    program_name="R2B",
)


# --- Alchemist ----------------------------------------------------------

ALCHEMIST_SORTING_CONFIG = SortingConfig(
    program="alchemist",
    sheet_id_env="ALCHEMIST_SHEET_ID",
    sheet_range_env="ALCHEMIST_SHEET_RANGE",
    program_name="Alchemist",
)


_PROGRAMS: dict[str, SortingConfig] = {
    "r2b": R2B_SORTING_CONFIG,
    "alchemist": ALCHEMIST_SORTING_CONFIG,
}


def get_program_config(name: str) -> SortingConfig:
    """Resolve a program config by name. Raises if unknown so a typo
    fails loudly at startup rather than silently reading the wrong sheet.
    """
    key = (name or "").strip().lower()
    if key not in _PROGRAMS:
        raise ValueError(
            f"Unknown program '{name}'. Known programs: {sorted(_PROGRAMS)}"
        )
    return _PROGRAMS[key]


def list_programs() -> list[str]:
    """Sorted list of program keys for UI dropdowns."""
    return sorted(_PROGRAMS)
