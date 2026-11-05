"""Open a pull request in the shop's repository through GitHub's REST API.

The shop's repository is separate from Sherlog's and its commits don't share
SHAs with the platform's, so the fixer brings the code itself: it commits the
code exactly as deployed (the tarball Sherlog sent) as the branch
sherlog/deployed-<sha>, on top of the repository's default branch, then the
fix on top of that. The pull request goes from the fix to the deployed
branch, so its diff is only the fix. No clone and no git push: one Git Data
API call per step.
"""

import httpx

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str, repo: str) -> None:
        self.repo = repo
        self._http = httpx.AsyncClient(
            base_url=f"{API}/repos/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "sherlog-fixer",
            },
            timeout=30.0,
        )

    async def _call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201), **kwargs) -> dict:
        response = await self._http.request(method, path, **kwargs)
        if response.status_code not in ok:
            raise GitHubError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    async def check(self) -> str:
        """The default branch's head commit; fails if the repo is missing, empty or not writable."""
        # Absolute URL: with an empty path httpx adds a trailing slash, and GitHub answers 404.
        repo = await self._call("GET", f"{API}/repos/{self.repo}", ok=(200,))
        if not (repo.get("permissions") or {}).get("push"):
            raise GitHubError(f"the token can't push to {self.repo}")
        ref = await self._call("GET", f"/git/ref/heads/{repo['default_branch']}", ok=(200,))
        return ref["object"]["sha"]

    async def _tree(self, files: dict[str, bytes | None], base_tree: str | None = None) -> str:
        entries = []
        for path, content in sorted(files.items()):
            entry = {"path": path, "mode": "100644", "type": "blob"}
            if content is None:
                entry["sha"] = None
            else:
                try:
                    entry["content"] = content.decode("utf-8")
                except UnicodeDecodeError:
                    import base64

                    blob = await self._call(
                        "POST", "/git/blobs", json={"content": base64.b64encode(content).decode(), "encoding": "base64"}
                    )
                    entry["sha"] = blob["sha"]
            entries.append(entry)
        body = {"tree": entries, **({"base_tree": base_tree} if base_tree else {})}
        return (await self._call("POST", "/git/trees", json=body))["sha"]

    async def _commit(self, message: str, tree: str, parent: str) -> str:
        commit = await self._call(
            "POST", "/git/commits",
            json={
                "message": message, "tree": tree, "parents": [parent],
                "author": {"name": "Sherlog", "email": "sherlog@users.noreply.github.com"},
            },
        )
        return commit["sha"]

    async def _branch_head(self, name: str) -> str | None:
        response = await self._http.get(f"/git/ref/heads/{name}")
        return response.json()["object"]["sha"] if response.status_code == 200 else None

    async def _create_branch(self, name: str, sha: str) -> None:
        await self._call("POST", "/git/refs", json={"ref": f"refs/heads/{name}", "sha": sha})

    async def open_pr(
        self, version: str, deployed: dict[str, bytes], fix: dict[str, bytes | None],
        branch: str, title: str, body: str, deploy_message: str, fix_message: str,
    ) -> dict:
        """deployed: the code as it ran; fix: changed paths (None deletes)."""
        head = await self.check()
        base_branch = f"sherlog/deployed-{version}"
        base_commit = await self._branch_head(base_branch)
        if base_commit is None:
            base_commit = await self._commit(deploy_message, await self._tree(deployed), head)
            await self._create_branch(base_branch, base_commit)
        base_tree = (await self._call("GET", f"/git/commits/{base_commit}", ok=(200,)))["tree"]["sha"]
        fix_commit = await self._commit(fix_message, await self._tree(fix, base_tree), base_commit)
        await self._create_branch(branch, fix_commit)
        pr = await self._call("POST", "/pulls", json={"title": title, "head": branch, "base": base_branch, "body": body})
        return {"url": pr["html_url"], "number": pr["number"], "branch": branch, "base": base_branch, "commit": fix_commit}
