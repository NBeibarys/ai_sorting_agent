"""ADK batch country-classifier: classifier -> verifier (no loop)."""
from google.adk.agents import Agent, SequentialAgent
from google.adk.models.google_llm import Gemini
from google.genai import types

from .prompts import build_head_instruction, build_sorter_instruction
from .schemas import BatchClassification, BatchVerdict


def build_root_agent(model: str, country_field_label: str = "") -> SequentialAgent:
    """Classifier -> verifier. Two LLM calls per batch, no correction loop.

    ``country_field_label`` is the actual sheet column label (e.g. "Which
    country do most of your team members come from?") injected into the
    classifier/verifier prompts so the description of ``country_raw`` matches
    the cohort's form question instead of a hardcoded phrasing.
    """
    sorter_instruction = build_sorter_instruction(country_field_label)
    head_instruction = build_head_instruction(country_field_label)
    classifier = Agent(
        name="classifier",
        description="Classifies a batch of startup HQ country strings into canonical buckets. Returns one classification per input row, keyed by row_id.",
        model=Gemini(model=model, retry_options=types.HttpRetryOptions(attempts=1)),
        instruction=sorter_instruction,
        output_schema=BatchClassification,
        output_key="batch_classifications",
        generate_content_config=types.GenerateContentConfig(temperature=0),
        timeout=180,
    )
    verifier = Agent(
        name="verifier",
        description="Independently verifies a batch of classifier buckets and supplies corrected buckets for any it rejects. Returns one verdict per input row, keyed by row_id.",
        model=Gemini(model=model, retry_options=types.HttpRetryOptions(attempts=1)),
        instruction=head_instruction,
        output_schema=BatchVerdict,
        output_key="batch_verdicts",
        generate_content_config=types.GenerateContentConfig(temperature=0),
        timeout=180,
    )
    return SequentialAgent(
        name="country_sorting",
        description="Batch country classification with independent verification.",
        sub_agents=[classifier, verifier],
    )
