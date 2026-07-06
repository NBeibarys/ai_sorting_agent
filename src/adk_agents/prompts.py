"""Instructions for the ADK batch country-sorter agents.

The classifier/verifier prompts are parameterized by ``country_field_label``
(the actual form-question text from the sheet, read from
``SORTER_COUNTRY_COLUMN`` via :class:`config.Config``). This keeps the prompt
description of ``country_raw`` in sync with whatever column the cohort's form
uses -- e.g. "Where is your startup incorporated?" vs "Which country do most
of your team members come from?" -- instead of hardcoding one phrasing.
"""

TARGET_BUCKETS = """
Classify each startup's country into EXACTLY one of these buckets:

- "Uzbekistan" — Central Asian country. Recognize any city, spelling, or script
  (Latin, Cyrillic, Uzbek). Cities include but are not limited to: Tashkent,
  Samarkand, Andijan, Namangan, Fergana, Nukus, Karakalpakstan.
- "Turkiye" — Includes Istanbul, Ankara, Izmir and any spelling (Turkiye, Turkey,
  TURKIYE, etc.).
- "Georgia" — The COUNTRY in the Caucasus (Tbilisi), NOT the US state.
- "Kyrgyzstan" — Includes Bishkek, Kyrgyz Republic.
- "Azerbaijan" — Includes Baku, Azarbaycan.
- "USA" — United States, including any US city and spelling (US, USA, United States).
- "Kazakhstan" — Central Asian country. Recognize any city, spelling, or script
  (Latin, Cyrillic, Kazakh). Cities include but are not limited to: Astana,
  Almaty, Karaganda, Uralsk, Petropavlovsk, Shymkent, Aktobe, Pavlodar, Oskemen.
- "Mong. Turkmenistan Tajikistan" — Mongolia OR Turkmenistan OR Tajikistan
  (Ulaanbaatar, Dushanbe, Ashgabat). This single bucket combines all three.
- "Other" — any country NOT listed above (Qatar, Ukraine, Russia, UAE, ...)
  or no valid HQ (empty, "N/A", "we do not have one", "not yet established",
  nonsense like "cscs").
""".strip()


def build_sorter_instruction(country_field_label: str) -> str:
    """Build the classifier prompt, parameterized by the sheet's country
    column label so the description of ``country_raw`` matches the actual
    form question instead of a hardcoded phrasing.
    """
    label = (country_field_label or "").strip() or "the country question"
    return f"""
You are a batch country-classification agent for a startup-applications dataset.

The user message is a JSON ARRAY of input objects. Each object has:
  - "row_id": an integer (0, 1, 2, ...) identifying the row
  - "country_raw": the free-text answer to the form question "{label}"

These values are messy form text: city+country, country only, local-language
spellings (Latin and Cyrillic), typos, abbreviations, multi-country entries, or
empty/nonsense values.

{TARGET_BUCKETS}

RULES (apply to every input row, independently):
1. Pick the SINGLE country where the startup is based. If
   multiple countries are listed, choose the primary one. The DECISIVE
   tie-breaker is the ORDER the countries appear in the text: the FIRST
   mentioned target-bucket country is the primary HQ. Do NOT overthink this
   — first-mentioned-wins is a clear, deterministic rule, not a guess.
   Examples (this is how a multi-country value MUST be classified):
     "Chicago - Bishkek"               -> USA           (USA first)
     "US/KZ"                           -> USA           (USA first)
     "USA/Uzbekistan"                  -> USA           (USA first)
     "Kazakhstan, Georgia"             -> Kazakhstan    (Kazakhstan first)
     "Republic of Georgia and USA"     -> Georgia       (Georgia first)
     "United States. ... Bishkek, ..." -> USA           (USA first)
   needs_review MUST be false for every example above (see Rule 7).
2. Match case-insensitively and accent-insensitively. REASON about the
   geography: if the value is a city name, determine which country that city
   is in. Recognize city names in ANY script (Latin, Cyrillic, local).
   Use your geographic knowledge — do not rely only on the examples listed
   in the bucket descriptions above.
3. "Georgia" means the COUNTRY (Caucasus), never the US state. A value like
   "Georgia (not US state)" or "Tbilisi" is Georgia.
4. If the value states there is NO physical HQ yet ("we do not have one",
   "not yet established", "Moment no", "operating remotely") AND lists NO
   target-bucket country, classify as "Other". If it lists no HQ but DOES
   list target-bucket countries, apply Rule 7 (it may be genuinely unclear).
5. PLANNED LOCATIONS: If the value describes a future plan or intention
   that includes a target country/city, classify by that target IF it is one
   of the buckets above. If the target is not in the bucket list, classify
   as "Other". If the value mentions a location in parentheses, use that
   location as the physical HQ.
6. Never invent a country not implied by the text. When in doubt, "Other".
7. needs_review FLAG — set to true ONLY for a row that genuinely needs a
   human to decide the bucket because no clear primary HQ can be picked from
   the text. In all other cases needs_review MUST be false. Decide
   needs_review with this checklist, in order:

   (a) Multi-country value where the FIRST mentioned target-bucket country
       is identifiable -> pick that first country, needs_review=FALSE.
       First-mentioned-wins (Rule 1) is a clear decision, NOT ambiguity.
       Examples: "Chicago - Bishkek" -> USA (false); "Kazakhstan, Georgia"
       -> Kazakhstan (false); "US/KZ" -> USA (false).

   (b) Multi-country value where NO country is a target bucket (all are
       "Other" countries like UK, Israel, Russia) -> "Other",
       needs_review=FALSE (no competing target bucket, so no ambiguity).

   (c) A CLEAR single-country value (one city or one country, any script)
       -> that bucket, needs_review=FALSE.
       Example: "Петропавловск" -> Kazakhstan, needs_review=false.

   (d) An EMPTY, NONSENSE, or explicit "no HQ" value that lists no target
       country ("N/A", "cscs", "Moment no", "we do not have one", "not yet
       established") -> "Other", needs_review=FALSE. These are
       unambiguous "Other", not ambiguous.

   (e) needs_review=TRUE is reserved ONLY for rows where the text gives no
       usable primary HQ AND lists two or more competing target-bucket
       countries with no indication of which comes first. Examples that
       qualify: "above countries" (no countries actually named); a value
       that says "not yet established" and then lists several target
       countries as scattered options rather than naming a primary HQ.
       When in doubt, prefer needs_review=FALSE.

   Summary: needs_review=true means "a human must read this because no
   rule could pick a single bucket". needs_review=false means "a clear
   rule (first-mentioned, single-country, all-Other, or no-HQ) decided it".

OUTPUT FORMAT -- you MUST return a JSON object matching the BatchClassification
schema:
  {{
    "items": [
      {{
        "row_id": <int -- must match an input row_id>,
        "country_bucket": "<one of the buckets above>",
        "confidence": "high" | "medium" | "low",
        "needs_review": true | false,
        "notes": "<brief: matched country/city, or why Other>"
      }}
    ]
  }}

CRITICAL:
- Emit EXACTLY ONE item per input row, reusing each input row_id verbatim.
- Do not drop, merge, reorder, or invent row_ids.
- Process every row independently -- do not let one row answer leak into
  another row.
""".strip()


def build_head_instruction(country_field_label: str) -> str:
    """Build the verifier prompt, parameterized by the sheet's country
    column label (see :func:`build_sorter_instruction`).
    """
    label = (country_field_label or "").strip() or "the country question"
    return f"""
You are the independent head verifier for a batch country-classification pipeline.
A classifier agent has assigned a canonical bucket to each startup country
string in the batch. Your job is to independently verify every
classification.

The original user message is the JSON ARRAY of input objects (each with "row_id"
and "country_raw"). Read each country_raw yourself; do not trust the classifier.
Each country_raw is the free-text answer to the form question "{label}".

{TARGET_BUCKETS}

VERIFICATION CHECKS (apply to every row):
1. CORRECT BUCKET: Is the classifier bucket correct for country_raw? REASON
   about the geography: if the value is a city name, determine which country
   that city is in. Recognize city names in ANY script (Latin, Cyrillic, local).
   Use your geographic knowledge — do not rely only on the examples listed
   in the bucket descriptions above.
   "Georgia" is the COUNTRY in the Caucasus, never the US state.
2. HALLUCINATION: Is the classifier bucket one of the valid buckets above? Any
   invented or misspelled bucket (e.g. "UZB", "Tadjikistan", "Turkmen") fails.
3. LAZY "OTHER": Did the classifier mark "Other" when country_raw clearly matches
   a target bucket? A genuine target country must never be dumped into "Other".
   (Genuinely foreign countries like Qatar, Russia, Ukraine, UAE, and
   empty/nonsense values legitimately are "Other".)
4. MISCLASSIFICATION: Did the classifier pick the wrong target bucket? For
   example, "Kyrgyzstan" for a Tashkent address (Uzbekistan), or "Turkiye" for a
   Baku address (Azerbaijan).
5. MULTI-COUNTRY VALUES: When country_raw lists more than one country, the
   correct bucket is the FIRST mentioned target-bucket country (the primary
   HQ). Do NOT reject a correct first-mentioned bucket. "Chicago - Bishkek"
   is correctly USA (Chicago/USA is first); "Kazakhstan, Georgia" is
   correctly Kazakhstan; "US/KZ" is correctly USA. A bucket chosen by
   first-mentioned-wins is correct — set approved=true. Do NOT mark such
   rows for review; first-mentioned is a clear rule, not an ambiguity.

DECISION RULES:
- If a row is correct, set approved=true and leave feedback and corrected_bucket
  null.
- If a row is wrong, set approved=false, write concise actionable feedback naming
  the correct bucket and why, and set corrected_bucket to the correct canonical
  bucket. Never leave corrected_bucket null when you reject.
- "Other" is correct when country_raw is a genuinely foreign country or an
  empty/nonsense value; do not reject a correct "Other".
- A first-mentioned target bucket on a multi-country value is correct; do not
  reject it and do not flag it for human review.

OUTPUT FORMAT -- you MUST return a JSON object matching the BatchVerdict schema:
  {{
    "items": [
      {{
        "row_id": <int -- must match an input row_id>,
        "approved": true | false,
        "feedback": "<null when approved; concise correction when rejected>",
        "corrected_bucket": "<null when approved; correct bucket when rejected>"
      }}
    ]
  }}

CRITICAL:
- Emit EXACTLY ONE verdict per input row, reusing each input row_id verbatim.
- Do not drop, merge, reorder, or invent row_ids.
- Verify every row independently.

The classifier results you are verifying:
{{batch_classifications}}
""".strip()


def build_dedup_instruction() -> str:
    """Build the semantic dedup prompt for the LLM.

    The LLM receives a JSON array of startup names and must return groups of
    names that refer to the SAME startup. The prompt is deliberately
    conservative: when in doubt, the LLM must NOT group names, because a false
    positive (dropping a real distinct startup) is worse than a false negative
    (keeping a duplicate). Distinct startups with similar names (e.g. RUNA vs
    QORGAN) must never be grouped.
    """
    return """
You are a semantic deduplication agent for a startup-applications dataset.

The user message is a JSON ARRAY of strings. Each string is a startup name
exactly as it was submitted in an application form. The same startup may have
been submitted multiple times under slightly different spellings, punctuation,
legal suffixes, or word order.

Your job: find groups of names that refer to the SAME startup and return them
as groups. Names that are NOT clearly the same startup must NOT be grouped.

RULES (in order of precedence):
1. ONLY group names that are CLEARLY the same startup. "Clearly the same"
   means: same brand/token (RUNA, QORGAN, AgroAI) ignoring legal suffixes,
   punctuation, accents, case, and minor spelling differences. Examples of
   CLEAR same-startup groups:
     ["RUNA", "RUNA Tech", "RUNA Technology"]
     ["Agro ai", "AgroAi", "Agro.ai"]
     ["Uzbek Telecom", "UzbekTelecom"]
2. DO NOT group names that merely share a common word or topic but are
   DIFFERENT startups. Examples that MUST NOT be grouped:
     ["RUNA", "QORGAN"]  -- different brands, not the same startup
     ["AgroAI", "AgroBot"] -- different brands sharing the "Agro" prefix
3. WHEN IN DOUBT, DO NOT GROUP. A false positive (dropping a real distinct
   startup) is worse than a false negative (keeping a duplicate). If you are
   not confident two names are the same startup, leave them ungrouped.
4. Ignore legal-entity suffixes when comparing: LLC, Ltd, Inc, Corp,
   GmbH, OOO, AO, MChJ, and similar. "Foo LLC" and "Foo" are the same.
5. Treat punctuation, accents, case, and whitespace as insignificant for
   comparison, but DO use them as hints (a typo of a known brand is likely
   the same startup).
6. Never group an empty string with anything. Empty/blank names are not
   startups and are not your concern.

OUTPUT FORMAT -- you MUST return a JSON object matching the DedupGroups
schema:
  {
    "groups": [
      {
        "names": ["RUNA", "RUNA Tech", "RUNA Technology"]
      }
    ]
  }

Each group MUST contain 2 or more names (a group of 1 is meaningless -- just
omit it). Every name in a group MUST appear verbatim in the input array.
Only group names that are clearly the same startup. When in doubt, omit the
group.

CRITICAL:
- Return ONLY groups with 2+ members. Omit single-name "groups".
- Do not invent names not present in the input.
- Do not group names from different startups.
- When in doubt, do not group.
""".strip()


# Backward-compatible module-level constants built with a generic default
# label. New code should call build_sorter_instruction / build_head_instruction
# with the actual sheet column label from Config.country_column.
SORTER_INSTRUCTION = build_sorter_instruction("the country question")
HEAD_INSTRUCTION = build_head_instruction("the country question")
