"""ADK batch country-classifier: classifier -> verifier (no loop)."""
import os

from google.adk.agents import Agent, SequentialAgent
from google.adk.models.google_llm import Gemini
from google.genai import types

from .prompts import HEAD_INSTRUCTION, SORTER_INSTRUCTION
from .schemas import BatchClassification, BatchVerdict


def build_root_agent(model: str) -> SequentialAgent:
    """Classifier -> verifier. Two LLM calls per batch, no correction loop."""
    classifier = Agent(
        name="classifier",
        description="Classifies a batch of startup HQ country strings into canonical buckets. Returns one classification per input row, keyed by row_id.",
        model=Gemini(model=model, retry_options=types.HttpRetryOptions(attempts=1)),
        instruction=SORTER_INSTRUCTION,
        output_schema=BatchClassification,
        output_key="batch_classifications",
        generate_content_config=types.GenerateContentConfig(temperature=0),
        timeout=180,
    )
    verifier = Agent(
        name="verifier",
        description="Independently verifies a batch of classifier buckets and supplies corrected buckets for any it rejects. Returns one verdict per input row, keyed by row_id.",
        model=Gemini(model=model, retry_options=types.HttpRetryOptions(attempts=1)),
        instruction=HEAD_INSTRUCTION,
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


root_agent = build_root_agent(os.environ.get("SORTER_MODEL", "gemini-3.5-flash"))
