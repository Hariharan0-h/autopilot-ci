"""git_tools.py — gitpython wrappers for AutoPilot CI pipeline."""

import os
from pathlib import Path
import httpx
import git
from git import Repo, Actor


def get_diff(repo_path: str, base_commit: str, head_commit: str) -> dict[str, str]:
    """Return {filename: unified_diff_string} for Python files changed between commits.

    Args:
        repo_path: Local path to the git repository.
        base_commit: Base commit SHA (before push).
        head_commit: Head commit SHA (after push).

    Returns:
        Dict mapping relative filepath to its unified diff string.
    """
    repo = Repo(repo_path)
    try:
        base = repo.commit(base_commit)
        head = repo.commit(head_commit)
    except git.BadName:
        # Fall back to HEAD~1..HEAD if SHAs are fake/demo values
        commits = list(repo.iter_commits(max_count=2))
        if len(commits) < 2:
            # Only one commit — diff against empty tree
            head = commits[0]
            diffs = head.diff(git.NULL_TREE, create_patch=True)
            result: dict[str, str] = {}
            for diff in diffs:
                if diff.b_path and diff.b_path.endswith(".py"):
                    result[diff.b_path] = diff.diff.decode("utf-8", errors="replace")
            return result
        head, base = commits[0], commits[1]

    diffs = base.diff(head, create_patch=True)
    result: dict[str, str] = {}
    for diff in diffs:
        path = diff.b_path or diff.a_path
        if path and path.endswith(".py"):
            result[path] = diff.diff.decode("utf-8", errors="replace")
    return result


def get_changed_files(repo_path: str, base: str, head: str) -> list[str]:
    """Return list of changed Python file paths (relative to repo root).

    Args:
        repo_path: Local path to the git repository.
        base: Base commit SHA.
        head: Head commit SHA.

    Returns:
        List of relative paths to changed .py files.
    """
    diff = get_diff(repo_path, base, head)
    return list(diff.keys())


def get_file_content(repo_path: str, filepath: str, commit: str = "HEAD") -> str:
    """Return the content of a file at a given commit.

    Args:
        repo_path: Local path to the git repository.
        filepath: Relative path to the file within the repo.
        commit: Commit SHA or ref (default: 'HEAD').

    Returns:
        File content as a string, or empty string if not found.
    """
    repo = Repo(repo_path)
    try:
        blob = repo.commit(commit).tree / filepath
        return blob.data_stream.read().decode("utf-8", errors="replace")
    except (KeyError, git.BadName, git.BadObject):
        # Fall back to reading from disk
        disk_path = Path(repo_path) / filepath
        if disk_path.exists():
            return disk_path.read_text(encoding="utf-8", errors="replace")
        return ""


def create_branch(repo_path: str, branch_name: str) -> None:
    """Create and check out a new branch.

    Args:
        repo_path: Local path to the git repository.
        branch_name: Name of the new branch to create.
    """
    repo = Repo(repo_path)
    repo.git.checkout("-b", branch_name)


def commit_changes(
    repo_path: str,
    files: list[str],
    message: str,
    author_name: str = "AutoPilot CI",
    author_email: str = "autopilot@ci.local",
) -> str:
    """Stage files, commit, and return the commit SHA.

    Args:
        repo_path: Local path to the git repository.
        files: List of relative file paths to stage.
        message: Commit message.
        author_name: Git author name.
        author_email: Git author email.

    Returns:
        The new commit SHA as a hex string.
    """
    repo = Repo(repo_path)
    repo.index.add(files)
    author = Actor(author_name, author_email)
    commit = repo.index.commit(message, author=author, committer=author)
    return commit.hexsha


def push_branch(repo_path: str, branch_name: str, remote: str = "origin") -> None:
    """Push a branch to the remote. Skips silently if no remote configured.

    Args:
        repo_path: Local path to the git repository.
        branch_name: Branch name to push.
        remote: Remote name (default: 'origin').
    """
    repo = Repo(repo_path)
    if not repo.remotes:
        return
    try:
        origin = repo.remote(remote)
        origin.push(refspec=f"{branch_name}:{branch_name}")
    except git.GitCommandError:
        # Remote push failed (no network, auth error, etc.) — skip silently
        pass


def open_pull_request(
    repo_url: str,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    github_token: str = "",
) -> str:
    """Open a PR via GitHub REST API. Returns the PR URL or empty string.

    If github_token is empty, prints the PR body to stdout and returns "".
    Parses owner/repo from repo_url automatically.

    Args:
        repo_url: GitHub repo URL (e.g. https://github.com/owner/repo).
        branch: Head branch with fixes.
        base_branch: Target branch (e.g. 'main').
        title: PR title.
        body: PR body in markdown.
        github_token: GitHub personal access token. Optional.

    Returns:
        PR URL string, or "" if token is absent or request fails.
    """
    if not github_token:
        print(f"\n{'='*60}")
        print(f"PR TITLE: {title}")
        print(f"BRANCH:   {branch} → {base_branch}")
        print(f"BODY:\n{body}")
        print(f"{'='*60}\n")
        return ""

    # Parse owner/repo from URL
    # Handles: https://github.com/owner/repo or https://github.com/owner/repo.git
    parts = repo_url.rstrip("/").rstrip(".git").split("/")
    if len(parts) < 2:
        return ""
    owner, repo = parts[-2], parts[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "body": body,
        "head": branch,
        "base": base_branch,
    }

    try:
        response = httpx.post(api_url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json().get("html_url", "")
    except httpx.HTTPError:
        return ""
