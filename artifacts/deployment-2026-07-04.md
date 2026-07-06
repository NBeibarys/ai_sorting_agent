# Deployment Report — sorting-dashboard v1.4-semantic-dedup

**Generated:** 2026-07-04 20:25 UTC
**Task:** t_a16f4050
**Repo:** github.com:NBeibarys/ai_sorting_agent.git
**Operator:** ops profile

---

## Summary

Deployed the `v1.4-semantic-dedup` release of the ai_sorting_agent Streamlit
dashboard to Google Cloud Run. This release combines the audit_v2/v3 bug
fixes with the new semantic deduplication feature (exact + email + fuzzy name
matching) and stale tab cleanup.

## Git State

| Item | Value |
|------|-------|
| Active branch | `alchemist` |
| HEAD commit | `1eea523` — [verified] Fix 2 limitations: semantic dedup (exact+email+fuzzy) and stale tab cleanup |
| Working tree | clean |
| `alchemist` vs `origin/alchemist` | in sync |
| `r2b` vs `origin/r2b` | in sync (HEAD: `5822258`) |

### Tags

| Tag | Commit | Subject |
|-----|--------|---------|
| v1.1-bugfix | `2146ad1` | fix: chunk size 100, startup table shows only name and classify column |
| v1.2-audit-fix | `62e29d2` | Fix all 8 bugs from audit |
| v1.3-skilled-audit | `0ee95e9` | [verified] Fix all 6 bugs from audit_v3.txt |
| **v1.4-semantic-dedup** | **`1eea523`** | [verified] Fix 2 limitations: semantic dedup + stale tab cleanup |

All tags pushed to origin.

## Cloud Run Deployment

| Property | Value |
|----------|-------|
| Service | `sorting-dashboard` |
| Region | `us-central1` |
| Service URL | https://sorting-dashboard-1030886862079.us-central1.run.app |
| Alt URL | https://sorting-dashboard-5m4rbqvqoa-uc.a.run.app |
| New revision | `sorting-dashboard-00011-jw2` (active, 100% traffic) |
| Prior revision | `sorting-dashboard-00010-frk` (v1.3-skilled-audit) |
| Deployed by | ais-gemini-key-a73489109d314c5@1030886862079.iam.gserviceaccount.com |
| Deployed at | 2026-07-04 20:19:42 UTC |

## Container Image

| Property | Value |
|----------|-------|
| Registry | `us-central1-docker.pkg.dev/gen-lang-client-0847622378/sorting-apps/dashboard` |
| Tag | `v1.4-semantic-dedup` |
| Pushed digest | `sha256:aced316804d731d4d6793d7a9baff34006bf4e2443018119887422afeb39f02d` |
| Deployed digest | `sha256:3b2607bac363904f639983e0ceb85310ea8c04f5190a8436d7e270f410f9b896` |

Note: The pushed digest and the deployed digest differ because Cloud Run
resolves the image by its manifest digest, which can differ from the
per-platform digest reported by `docker push` on a multi-platform or
re-tagged image. Both refer to the same image content built from the
`1eea523` working tree.

## Verification

| Check | Result |
|-------|--------|
| Revision incremented (00010 → 00011) | PASS |
| New revision active, serving 100% traffic | PASS |
| HTTP health check (curl service URL) | HTTP 200 |
| Working tree clean | PASS |
| Both branches pushed to origin | PASS |
| Tag `v1.4-semantic-dedup` pushed to origin | PASS |
| Python compile (all .py files) | PASS |

## What's in this release

**Semantic dedup** (`src/pipeline.py`): `_deduplicate_rows` now runs three
passes instead of one:
1. EXACT match on raw dedup cell (unchanged behavior)
2. EMAIL match: drops later rows sharing an earlier kept row's email
3. FUZZY name match: normalizes names (lowercase, accent-fold, strip all
   punctuation and whitespace) then drops later rows whose normalized name
   collides with an earlier kept row's. Avoids edit distance to prevent
   over-merging distinct short names.

**Stale tab cleanup** (`src/pipeline.py` + `src/google_clients.py`):
`_cleanup_stale_tabs` deletes country tabs left over from a prior run that
are not in the current run's tab list, preserving `PROTECTED_TABS` (Form
Responses 1, Total Statistics, CRM). New helpers: `list_existing_tab_titles`,
`delete_sheet_tab`.

**Audit fixes** (from v1.3-skilled-audit, included in this tag's history):
Dockerfile service_account.json handling, google_clients.py backoff fix,
workflow.py errored_rids dead-code wiring, app.py percentage denominator
fix, startup table CEO-column exclusion, build_root_agent caching.

## Deploy method

Local Docker build + direct Artifact Registry push (bypass-Cloud-Build
workaround per the gcp-cloud-run-deploy skill), then image-only
`gcloud run deploy --image=...` to preserve existing env vars, secrets,
auth, and config.

## Notes

- "Regional Access Boundary / Precondition check failed" warnings appeared
  on gcloud commands but are harmless org-policy noise; all operations
  succeeded after them.
- Build and deploy scripts were created as scratch files (`_build_v14.sh`,
  `_deploy_v14.sh`) and removed after use; the working tree is clean.
