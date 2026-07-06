"""
Central config, env-driven only — no hardcoded secrets or sheet IDs.
Fail fast at startup (not mid-batch) if required env vars are missing,
since a run that dies partway from a missing var wastes real API spend.

Vertex AI only (GOOGLE_GENAI_USE_VERTEXAI=TRUE) — the Gemini Developer
API path is intentionally not supported here. Reads from and writes back
to the SAME Google Sheet via a service account, mirroring the sibling
ai_fellowship_agent repo's config/validation pattern.

Multiple programs coexist (R2B, Alchemist). The active program is
selected via the ``program`` arg (CLI ``--program`` or the Streamlit
sidebar selector); each program owns its own sheet-id / sheet-range
env-var names so the two sheets never collide.
"""
import os
from dataclasses import dataclass

from .programs import SortingConfig, get_program_config


@dataclass(frozen=True)
class Config:
    sheet_id: str
    sheet_range: str
    service_account_path: str
    use_vertex: bool
    google_cloud_project: str
    google_cloud_location: str
    model: str
    checkpoint_path: str
    country_column: str
    name_column: str
    founder_name_column: str
    email_column: str
    telegram_column: str
    pitch_deck_column: str
    dedup_column: str
    llm_dedup_enabled: bool = True
    program_config: SortingConfig = None  # type: ignore[assignment]

    @classmethod
    def from_env(cls, program: str = "alchemist") -> "Config":
        """Build a Config from env vars for the given program.

        ``program`` selects which SortingConfig (and thus which
        sheet_id_env / sheet_range_env) is used. Defaults to
        'alchemist' for backwards compatibility.
        """
        program_config = get_program_config(program)
        sheet_id = os.environ.get(program_config.sheet_id_env, "")
        if not sheet_id:
            raise RuntimeError(f"{program_config.sheet_id_env} not set")

        service_account_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not service_account_path or not os.path.isfile(service_account_path):
            raise RuntimeError(
                f"GOOGLE_APPLICATION_CREDENTIALS not set or not found: {service_account_path}"
            )

        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"
        if not use_vertex:
            raise RuntimeError(
                "GOOGLE_GENAI_USE_VERTEXAI must be TRUE — this pipeline uses Vertex AI, "
                "not the Gemini Developer API. Set GOOGLE_GENAI_USE_VERTEXAI=TRUE."
            )

        google_cloud_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not google_cloud_project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT not set for Vertex AI")

        google_cloud_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "")
        if not google_cloud_location:
            raise RuntimeError("GOOGLE_CLOUD_LOCATION not set for Vertex AI")

        return cls(
            sheet_id=sheet_id,
            sheet_range=os.environ.get(
                program_config.sheet_range_env, program_config.default_sheet_range
            ),
            service_account_path=service_account_path,
            use_vertex=True,
            google_cloud_project=google_cloud_project,
            google_cloud_location=google_cloud_location,
            model=os.environ.get("SORTER_MODEL", "gemini-3.5-flash"),
            checkpoint_path=os.environ.get("CHECKPOINT_PATH", "checkpoint.json"),
            country_column=os.environ.get(
                "SORTER_COUNTRY_COLUMN",
                "which country do most of your team members come from",
            ),
            name_column=os.environ.get("SORTER_NAME_COLUMN", "startup name"),
            founder_name_column=os.environ.get(
                "SORTER_FOUNDER_NAME_COLUMN", "full name of your ceo"
            ),
            email_column=os.environ.get("SORTER_EMAIL_COLUMN", "ceo's email"),
            telegram_column=os.environ.get("SORTER_TELEGRAM_COLUMN", "telegram account"),
            pitch_deck_column=os.environ.get(
                "SORTER_PITCH_DECK_COLUMN", "pitch deck"
            ),
            dedup_column=os.environ.get("DEDUP_COLUMN", "Startup name"),
            llm_dedup_enabled=os.environ.get("LLM_DEDUP_ENABLED", "TRUE").upper() == "TRUE",
            program_config=program_config,
        )
