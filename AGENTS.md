# AI Sorting Agent — Project Rules

## CRITICAL: Dockerfile + .dockerignore

- `Dockerfile` MUST contain `COPY service_account.json .` after `COPY app.py .`
- `.dockerignore` MUST have `service_account.json` COMMENTED OUT (not ignored)
- `service_account.json` is gitignored via `*.json` in .gitignore — NEVER push to GitHub
- Before committing, verify: `grep "COPY service_account" Dockerfile` returns the COPY line

If either file is wrong, Cloud Run deployment breaks.
