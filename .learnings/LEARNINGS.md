## LESSON: Dockerfile and .dockerignore must include service_account.json

**What happened (Jul 4, 2026):**
The coder was tasked with fixing the Startup Table UI (country dropdown filter). While fixing app.py, the coder's commit accidentally reverted the Dockerfile and .dockerignore changes that were needed to include service_account.json in the Docker image.

**Root cause:**
The coder committed from a working tree that had the OLD Dockerfile and .dockerignore (before the manual fixes). This overwrote:
1. Dockerfile: lost the `COPY service_account.json .` line
2. .dockerignore: re-added `service_account.json` to the ignore list

**Impact:**
Cloud Run deployment broke — app couldn't read Google Sheets because service_account.json was missing from the container. Error: "GOOGLE_APPLICATION_CREDENTIALS not set or file not found: /app/service_account.json"

**Rule:**
- service_account.json MUST be in .dockerignore COMMENTED OUT (not ignored)
- Dockerfile MUST have `COPY service_account.json .` after `COPY app.py .`
- .gitignore MUST keep `*.json` so the secret is NEVER pushed to GitHub
- Before committing, ALWAYS check: `grep service_account Dockerfile` returns the COPY line
- NEVER revert Dockerfile or .dockerignore changes when committing app.py changes

**The correct state:**
- .gitignore: `*.json` (blocks from git) ✅
- .dockerignore: `# service_account.json - needed in image for Cloud Run` (commented out, NOT ignored) ✅
- Dockerfile: `COPY service_account.json .` (included in image) ✅
