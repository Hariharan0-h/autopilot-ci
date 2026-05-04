"""
demo/seed_bugs.py — Initialize sample_repo as a git repo with seeded bugs.

Runs from the autopilot-ci project root.
Creates an initial commit so the pipeline has something to diff against.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

SAMPLE_REPO = Path(__file__).parent / "sample_repo"


def run(cmd: list[str], cwd: Path) -> None:
    """Run a shell command, printing it first. Exit on failure."""
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}", file=sys.stderr)
        # Non-fatal for git config errors
    elif result.stdout.strip():
        print(f"  {result.stdout.strip()}")


def seed() -> None:
    """Initialize sample_repo with git and make an initial commit."""
    print("\n🌱 Seeding demo/sample_repo/ with intentional bugs...\n")

    # Check files exist
    for fname in ("app.py", "utils.py", "requirements.txt"):
        if not (SAMPLE_REPO / fname).exists():
            print(f"  ❌ Missing {fname} — run from autopilot-ci/ root.", file=sys.stderr)
            sys.exit(1)

    # Initialize git repo
    git_dir = SAMPLE_REPO / ".git"
    if git_dir.exists():
        print("  ℹ️  Git repo already exists — re-using.")
    else:
        run(["git", "init"], cwd=SAMPLE_REPO)

    # Set local git config
    run(["git", "config", "user.name", "AutoPilot Demo"], cwd=SAMPLE_REPO)
    run(["git", "config", "user.email", "demo@autopilot.ci"], cwd=SAMPLE_REPO)

    # Add all files
    run(["git", "add", "."], cwd=SAMPLE_REPO)

    # Check if there's already a commit
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=str(SAMPLE_REPO), capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        print("  ℹ️  Initial commit already exists.")
    else:
        run(["git", "commit", "-m", "feat: initial sample app with seeded bugs"], cwd=SAMPLE_REPO)

    print("\n✅ Sample repo ready!\n")
    print("Seeded bugs:")
    print("  1. 🔐 SQL injection via f-string in app.py:get_user (security_scanner will find)")
    print("  2. ⚡ O(n²) nested loop in app.py:find_duplicates (perf_analyzer will find)")
    print("  3. 🧪 Untested function calculate_discount in utils.py (test_generator will find)")
    print("  4. 🧪 Untested function validate_email in utils.py (test_generator will find)")
    print("  5. 📦 requests==2.18.0 CVE-2023-32681 in requirements.txt (security_scanner will find)")

    print("\n─────────────────────────────────────────────────────")
    print("🚀 Quick start:")
    print()
    print("  Terminal 1:  uvicorn server.webhook:app --port 8000 --reload")
    print("  Terminal 2:  python dashboard/app.py")
    print("  Browser:     http://localhost:7860")
    print("               → Click 'Trigger Demo Run'")
    print()
    print("Or run everything at once:")
    print("  make demo")
    print("─────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    seed()
