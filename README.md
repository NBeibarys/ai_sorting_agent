# AI Sorting Agent

Agentic system that sorts startup applications by physical HQ country and
writes the results back into the SAME Google Sheet it reads from — one new
tab per target country (columns: Startup Name, Timestamp). Built on Google
ADK with Vertex AI, mirroring the architecture of the sibling
ai_fellowship_agent repo.

## What it does

1. Reads application rows from a Google Sheet (the Google Forms responses
   tab) via a service account.
2. For each row, an ADK agent classifies the messy "physically headquartered"
   free-text value into one of eight canonical buckets: Uzbekistan, Turkiye,
   Georgia, Kyrgyzstan, Azerbaijan, USA, Kazakhstan, and a combined
   Mongolia/Turkmenistan/Tajikistan tab.
3. Creates one tab per bucket IN THE SAME SPREADSHEET and writes Startup Name
   + Timestamp for each matching startup. Startups outside the target list
   are classified "Other" and reported on stdout (no tab written for them).

The country column is intentionally LLM-classified (not regex-matched)
because the data is extremely messy: many distinct spellings across hundreds
of rows, multiple languages (Latin/Cyrillic), city+country combinations,
typos, and multi-country entries.

## Setup

1. Install dependencies:

       pip install -r requirements.txt

2. Place a service account JSON at `service_account.json` (or set
   `GOOGLE_APPLICATION_CREDENTIALS` to its path) and give the service
   account's email Editor access on the target Google Sheet.

3. Copy `.env.example` to `.env` and fill in `SORTER_SHEET_ID` (the target
   spreadsheet ID) and `GOOGLE_CLOUD_PROJECT` (your Vertex AI project).

4. Run the agent:

       python -m src.main

## Options

       python -m src.main --dry-run    # local heuristic, no LLM API calls (verification)
       python -m src.main --force      # re-classify every row, ignore checkpoint

## Configuration (.env)

- `GOOGLE_APPLICATION_CREDENTIALS`: path to the service account JSON.
- `GOOGLE_GENAI_USE_VERTEXAI=TRUE` + `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION`:
  Vertex AI backend (required; the Gemini Developer API is not used).
- `SORTER_SHEET_ID`: the spreadsheet to read from AND write tabs back into.
- `SORTER_SHEET_RANGE`: the responses tab name (default `Form Responses 1`).
- `SORTER_MODEL`: the Gemini model for the classifier (default gemini-3.5-flash).
- `MAX_CONCURRENCY`: parallel LLM calls (default 8).
- `CHECKPOINT_PATH`: resume a partial run without re-classifying finished rows.
