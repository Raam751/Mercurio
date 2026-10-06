"""Deploy the copilot to a Hugging Face Docker Space.

What it does:
1. Exports the COMMITTED files only (``git archive HEAD``), so ``.env`` and other
   untracked files can never be uploaded.
2. Adds the Space configuration header (sdk: docker, app_port) to README.md in the
   exported copy only. The GitHub README is unchanged.
3. Creates the Space if needed and stores KIRO_API_KEY as a Space *secret*
   (read from your environment or .env; never printed, never committed).
4. Uploads the files in one commit; Hugging Face then builds the Dockerfile.

Prerequisites (one time):
    pip install huggingface_hub
    hf auth login            # paste a token with "write" access from huggingface.co/settings/tokens

Usage:
    python deploy/deploy_hf_space.py --space <hf-username>/mercurio
    python deploy/deploy_hf_space.py --space <hf-username>/mercurio --no-llm   # rules-only, no secret
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SPACE_HEADER = """---
title: Mercurio Procurement Copilot
emoji: 🧾
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 8501
pinned: false
short_description: AI procurement copilot with human approval
---

"""


def export_committed_files(target: Path) -> None:
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True)
    if status.stdout.strip():
        print("Note: uncommitted changes exist and will NOT be deployed (only HEAD is uploaded).")
    archive = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(target, filter="data")
    if (target / ".env").exists():
        raise SystemExit("Refusing to deploy: .env is committed to git. Remove it from the repo first.")
    readme = target / "README.md"
    readme.write_text(SPACE_HEADER + readme.read_text(encoding="utf-8"), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--space", required=True, help="Space ID, e.g. your-hf-username/mercurio")
    parser.add_argument("--private", action="store_true", help="create the Space as private")
    parser.add_argument("--no-llm", action="store_true", help="do not set KIRO_API_KEY (rules-only deployment)")
    args = parser.parse_args()

    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise SystemExit("Install the Hugging Face client first: pip install huggingface_hub")

    api = HfApi()
    try:
        user = api.whoami()["name"]
    except Exception:  # noqa: BLE001
        raise SystemExit("Not logged in to Hugging Face. Run: hf auth login")
    print(f"Logged in to Hugging Face as {user}")

    api.create_repo(args.space, repo_type="space", space_sdk="docker", private=args.private, exist_ok=True)
    print(f"Space ready: {args.space}")

    if not args.no_llm:
        from dotenv import dotenv_values

        key = os.getenv("KIRO_API_KEY") or dotenv_values(ROOT / ".env").get("KIRO_API_KEY")
        if not key:
            raise SystemExit("KIRO_API_KEY not found in the environment or .env (use --no-llm for rules-only).")
        api.add_space_secret(args.space, "KIRO_API_KEY", key, description="Kiro CLI API key for the copilot agents")
        print("Stored KIRO_API_KEY as a Space secret (value not shown).")

    with tempfile.TemporaryDirectory(prefix="hf-space-") as tmp:
        export_committed_files(Path(tmp))
        commit = api.upload_folder(repo_id=args.space, repo_type="space", folder_path=tmp,
                                   commit_message="Deploy from GitHub HEAD")
        print(f"Uploaded: {commit.commit_url}")

    owner, name = args.space.split("/", 1)
    print(f"\nBuild logs: https://huggingface.co/spaces/{args.space}?logs=build")
    print(f"App URL (after the build, ~5 min): https://{owner.lower()}-{name.lower().replace('_', '-')}.hf.space")


if __name__ == "__main__":
    sys.exit(main())
