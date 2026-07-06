# AI Sorting Agent — Project Rules

## CODING STANDARDS

Code as a senior data scientist. Every line you write should be production-quality.

- **Explanatory comments**: Explain WHY, not WHAT. Future readers (human or AI) should understand the reasoning behind non-obvious decisions.
- **Best state-of-art techniques**: Use official SDKs, type hints, dataclasses, Pydantic schemas. No deprecated APIs.
- **No redundancy**: Don't repeat logic. DRY. If a pattern appears 3 times, abstract it.
- **No local imports**: NEVER use `import X` inside a function. It causes `UnboundLocalError` in Python because the variable becomes local-scoped. Always import at module top.
- **No hardcoded values**: Config-driven via env vars or dataclasses. Sheet IDs, model names, credentials all come from environment.
- **Type hints**: All function signatures should have type hints.
- **Error handling**: Catch specific exceptions, not bare `except:`. Log with context.
- **Testing**: Verify with `py_compile` before committing. Run actual tests, not just "it should work."

## CRITICAL: Deployment Workflow

Ops workers MUST follow this exact sequence before deploying:

1. **Commit first**: `git add -A && git commit -m "deploy: <description>"` — the Docker image is built from the git repo, NOT the working directory. Uncommitted changes are INVISIBLE to the build.
2. **Build**: `docker build -t us-central1-docker.pkg.dev/gen-lang-client-0847622378/sorting-apps/dashboard:latest .`
3. **Push**: `docker push us-central1-docker.pkg.dev/gen-lang-client-0847622378/sorting-apps/dashboard:latest`
4. **Deploy**: `gcloud run services replace service.yaml --region us-central1`
5. **Verify**: `curl -s -o /dev/null -w "%{http_code}" https://sorting-dashboard-1030886862079.us-central1.run.app` — must return 200

NEVER build the Docker image without committing first. Uncommitted coder changes will be lost.

## CRITICAL: Dockerfile + .dockerignore

- `Dockerfile` MUST NOT contain `COPY service_account.json .` — the service account is mounted via Secret Manager at `/secrets/service_account.json`
- `.dockerignore` MUST have `service_account.json` COMMENTED OUT (not ignored)
- `service_account.json` is gitignored via `*.json` in .gitignore — NEVER push to GitHub
- Service account is provided via Secret Manager secret `sorting-sa-key`, mounted as a volume at `/secrets/`

## CRITICAL: service.yaml

- `service.yaml` is the single source of truth for Cloud Run deployment
- ALL env vars, secrets, ports, and config are defined there
- Deploy with: `gcloud run services replace service.yaml --region us-central1`
- NEVER use `gcloud run deploy` with `--set-env-vars` — it replaces ALL env vars and drops existing ones
- To update the image: edit the `image:` line in service.yaml, then run the replace command
