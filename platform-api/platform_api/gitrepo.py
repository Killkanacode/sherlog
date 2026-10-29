"""Read-only access to the demo app's git history.

GIT_DIR points at the repository's .git directory (mounted read-only). Only
content under demo-app/ is ever exposed, and ref names are never returned:
consumers see commit SHAs, subjects and file paths, like a CD system would.
"""

import subprocess

DEMO_ROOT = "demo-app"


class GitError(Exception):
    pass


def _git(*args: str, binary: bool = False):
    result = subprocess.run(["git", *args], capture_output=True, check=False)
    if result.returncode != 0:
        raise GitError(result.stderr.decode(errors="replace").strip())
    return result.stdout if binary else result.stdout.decode(errors="replace")


def resolve(ref: str) -> str:
    """Full commit SHA for a branch, tag or (abbreviated) SHA."""
    return _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()


def commit_info(sha: str) -> dict:
    subject, author, committed_at = _git("log", "-1", "--format=%s%x00%an%x00%cI", sha).strip().split("\0")
    return {"sha": sha, "subject": subject, "author": author, "committed_at": committed_at}


def archive(sha: str) -> bytes:
    """Tarball of demo-app/ at a commit, used as the docker build context."""
    return _git("archive", "--format=tar", f"{sha}:{DEMO_ROOT}", binary=True)


def service_paths(service: str) -> list[str]:
    """Paths whose changes affect a service's image."""
    return [
        f"{DEMO_ROOT}/{service}",
        f"{DEMO_ROOT}/common",
        f"{DEMO_ROOT}/db/{service}",
        f"{DEMO_ROOT}/requirements.txt",
        f"{DEMO_ROOT}/Dockerfile",
    ]


def changed_files(from_sha: str, to_sha: str, service: str) -> list[str]:
    out = _git("diff", "--name-only", from_sha, to_sha, "--", *service_paths(service))
    return [line for line in out.splitlines() if line]


def diff(from_sha: str, to_sha: str, service: str, max_bytes: int = 30_000) -> str:
    out = _git("diff", "--unified=3", from_sha, to_sha, "--", *service_paths(service))
    if len(out) > max_bytes:
        out = out[:max_bytes] + "\n... diff truncated ...\n"
    return out


def show(sha: str, path: str) -> str:
    if not path.startswith(f"{DEMO_ROOT}/") or ".." in path.split("/"):
        raise GitError(f"path outside {DEMO_ROOT}/: {path}")
    return _git("show", f"{sha}:{path}")
