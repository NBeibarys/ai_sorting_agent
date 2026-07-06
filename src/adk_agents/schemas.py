"""Pydantic schemas for the batch country-classification agents."""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

COUNTRY_BUCKETS = (
    "Uzbekistan", "Turkiye", "Georgia", "Kyrgyzstan", "Azerbaijan",
    "USA", "Kazakhstan", "Mong. Turkmenistan Tajikistan", "Other",
)

CountryBucket = Literal[
    "Uzbekistan", "Turkiye", "Georgia", "Kyrgyzstan", "Azerbaijan",
    "USA", "Kazakhstan", "Mong. Turkmenistan Tajikistan", "Other",
]


class CountryClassification(BaseModel):
    """One row classification. Carries row_id so batch results map back."""
    model_config = ConfigDict(extra="ignore")
    row_id: int = Field(description="0-indexed row_id from the input; must match an input row_id.")
    country_bucket: CountryBucket = Field(description="Canonical HQ country bucket. Use 'Other' for any non-target country or empty/nonsense values.")
    confidence: Literal["high", "medium", "low"]
    needs_review: bool = Field(default=False, description="True when the country value is ambiguous (multiple countries listed, not yet established, unclear, or empty). The row is routed to a Human Review tab instead of a bucket.")
    notes: Optional[str] = None


class VerificationVerdict(BaseModel):
    """One row verification. Carries row_id so batch results map back."""
    model_config = ConfigDict(extra="ignore")
    row_id: int = Field(description="0-indexed row_id from the input; must match an input row_id.")
    approved: bool = Field(default=False, description="True only when the classifier bucket is correct.")
    feedback: Optional[str] = Field(default=None, description="Actionable correction when approved is false. Null when approved.")
    corrected_bucket: Optional[CountryBucket] = Field(default=None, description="Correct canonical bucket when approved is false. Null when approved.")


class BatchClassification(BaseModel):
    """Wraps a list of CountryClassification items, one per input row."""
    model_config = ConfigDict(extra="ignore")
    items: list[CountryClassification] = Field(default_factory=list, description="One classification per input row, each carrying its row_id.")


class BatchVerdict(BaseModel):
    """Wraps a list of VerificationVerdict items, one per input row."""
    model_config = ConfigDict(extra="ignore")
    items: list[VerificationVerdict] = Field(default_factory=list, description="One verdict per input row, each carrying its row_id.")


class DedupGroup(BaseModel):
    """A group of compound entries that refer to the same startup+founder.

    Each entry must be a verbatim compound string from the input array, in
    the form "Startup Name | Founder Name | Email" (founder/email may be
    absent for sheets without those columns, in which case the entry is just
    the startup name). Groups with fewer than 2 members are meaningless and
    must be omitted by the model.
    """
    model_config = ConfigDict(extra="ignore")
    names: list[str] = Field(
        description="Two or more compound entries (verbatim from the input, each 'Startup Name | Founder Name | Email') that refer to the SAME startup from the SAME founder."
    )


class DedupGroups(BaseModel):
    """Wraps the list of dedup groups returned by the semantic dedup agent."""
    model_config = ConfigDict(extra="ignore")
    groups: list[DedupGroup] = Field(
        default_factory=list,
        description="Groups of compound entries (Name | Founder | Email) referring to the same startup from the same founder. Omit single-entry groups.",
    )
