"""CLI entry point for the ADK startup country sorter."""
import argparse
import sys

from dotenv import load_dotenv

from .config import Config
from .pipeline import run_batch


def main():
    parser = argparse.ArgumentParser(
        description="Sort startups by HQ country into Google Sheet tabs."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify with a local heuristic instead of the LLM; no API calls (reads the sheet, no sheet writes).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-classify every row, ignoring the checkpoint.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process first N rows (0 = all)",
    )
    args = parser.parse_args()

    load_dotenv(override=False)
    config = Config.from_env()
    print(f"Sheet:  {config.sheet_id} (tab {config.sheet_range!r})")
    print(f"Model:  {config.model}  (dry_run={args.dry_run}, vertex={config.use_vertex})")

    result = run_batch(config, dry_run=args.dry_run, force=args.force, limit=args.limit)
    classified = result["classified"]
    errors = result["errors"]
    excluded = result["excluded"]
    print(f"\nClassified: {classified} | Errors: {len(errors)} | Excluded: {excluded}")
    for row_id, err in errors.items():
        print(f"  FAILED {row_id}: {err}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
