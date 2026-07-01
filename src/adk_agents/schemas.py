"""Pydantic schemas for the country-classification agents."""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Canonical target buckets, in tab order. The last combines Mongolia,
# Turkmenistan, and Tajikistan into one Excel tab. "Other" catches every
# country outside the target list (Qatar, Ukraine, Russia, ...) and invalid
# entries — those are excluded from the output tabs but reported on stdout.
COUNTRY_BUCKETS = (
    "Uzbekistan",
    "Turkiye",
    "Georgia",
    "Kyrgyzstan",
    "Azerbaijan",
    "USA",
    "Kazakhstan",
    "Mong. Turkmenistan Tajikistan",
    "Other",
)

# Excel sheet names are capped at 31 characters; every value above fits.
CountryBucket = Literal[
    "Uzbekistan",
    "Turkiye",
    "Georgia",
    "Kyrgyzstan",
    "Azerbaijan",
    "USA",
    "Kazakhstan",
    "Mong. Turkmenistan Tajikistan",
    "Other",
]


class CountryClassification(BaseModel):
    """Permissive transport shape so incomplete output still reaches the caller."""

    model_config = ConfigDict(extra="ignore")
    country_bucket: CountryBucket = Field(
        description=(
            "Canonical HQ country bucket. Use 'Other' for any country not in "
            "the target list or for empty/nonsense values."
        )
    )
    confidence: Literal["high", "medium", "low"]
    notes: Optional[str] = None


class VerificationVerdict(BaseModel):
    """Independent verification of a classifier country-bucket assignment.

    Used only for loop routing and classifier correction. When the classifier
    is wrong, the verifier supplies the corrected bucket so the gate can route
    a revision (or, after exhaustion, the workflow can fall back to it).
    """

    model_config = ConfigDict(extra="ignore")
    approved: bool = Field(
        default=False,
        description="True only when the classifier bucket is correct.",
    )
    feedback: Optional[str] = Field(
        default=None,
        description=(
            "Actionable correction instructions when approved is false. "
            "Null when approved is true."
        ),
    )
    corrected_bucket: Optional[CountryBucket] = Field(
        default=None,
        description=(
            "The correct canonical bucket when approved is false. Null when "
            "approved is true."
        ),
    )
