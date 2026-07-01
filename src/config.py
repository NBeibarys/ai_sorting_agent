"""
Central config, env-driven only — no hardcoded secrets or sheet IDs.
Fail fast at startup (not mid-batch) if required env vars are missing,
since a run that dies partway from a missing var wastes real API spend.

Vertex AI only (GOOGLE_GENAI_USE_VERTEXAI=TRUE) — the Gemini Developer
API path is intentionally not supported here. Reads from and writes back
to the SAME Google Sheet via a service account, mirroring the sibling
ai_fellowship_agent repo's config/validation pattern.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    sheet_id: str
    sheet_range: str
    service_account_path: str
    use_vertex: bool
    google_cloud_project: str
    google_cloud_location: str
    model: str
    max_concurrency: int
    checkpoint_path: str
    country_column: str
    name_column: str
    founder_name_column: str
    email_column: str
    telegram_column: str
    pitch_deck_column: str

    @classmethod
    def from_env(cls) -> "Config":
        sheet_id = os.environ.get("SORTER_SHEET_ID", "")
        if not sheet_id:
            raise RuntimeError("SORTER_SHEET_ID not set")

        service_account_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not service_account_path or not os.path.isfile(service_account_path):
            raise RuntimeError(
                f"GOOGLE_APPLICATION_CREDENTIALS not set or not found: {service_account_path}"
            )

        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"
        if not use_vertex:
            raise RuntimeError(
                "GOOGLE_GENAI_USE_VERTEXAI must be TRUE — this pipeline uses Vertex AI, "
                "not the Gemini Developer API."
            )

        google_cloud_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not google_cloud_project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT not set for Vertex AI")

        google_cloud_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "")
        if not google_cloud_location:
            raise RuntimeError("GOOGLE_CLOUD_LOCATION not set for Vertex AI")

        return cls(
            sheet_id=sheet_id,
            sheet_range=os.environ.get("SORTER_SHEET_RANGE", "Form Responses 1"),
            service_account_path=service_account_path,
            use_vertex=True,
            google_cloud_project=google_cloud_project,
            google_cloud_location=google_cloud_location,
            model=os.environ.get("SORTER_MODEL", "gemini-3.5-flash"),
            max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "8")),
            checkpoint_path=os.environ.get("CHECKPOINT_PATH", "checkpoint.json"),
            country_column=os.environ.get(
                "SORTER_COUNTRY_COLUMN", "physically headquartered"
            ),
            name_column=os.environ.get("SORTER_NAME_COLUMN", "name of your startup"),
            founder_name_column=os.environ.get(
                "SORTER_FOUNDER_NAME_COLUMN", "what is your name"
            ),
            email_column=os.environ.get("SORTER_EMAIL_COLUMN", "your email address"),
            telegram_column=os.environ.get(
                "SORTER_TELEGRAM_COLUMN", "your telegram handle"
            ),
            pitch_deck_column=os.environ.get(
                "SORTER_PITCH_DECK_COLUMN", "pitch deck"
            ),
        )
