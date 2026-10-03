"""Git tagging, including the phoenix/phoenix-data-gateway submodule special case."""
import logging
import os
import sys

from kald_devops.common import http_get, http_post, http_patch, make_gh_headers, require_env_vars, extract_gh_error

log = logging.getLogger(__name__)


def _gh_resolve_branch(gh_headers, repo, branch):
    url = f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}"
    resp = http_get(url, headers=gh_headers)
    if resp.status_code != 200:
        log.error("Failed to resolve branch '%s' in %s: %s - %s", branch, repo, resp.status_code, extract_gh_error(resp))
        return None
    return resp.json()["object"]["sha"]


def _gh_resolve_tag(gh_headers, repo, tag):
    """Resolve an existing tag to its commit SHA, dereferencing an annotated tag object
    if needed. Returns None (silently -- absence just means "not a tag") if it doesn't exist."""
    resp = http_get(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}", headers=gh_headers)
    if resp.status_code != 200:
        return None
    obj = resp.json()["object"]
    if obj.get("type") != "tag":
        return obj["sha"]
    # Annotated tag -- the ref's "object" is the tag object itself, not the commit.
    deref = http_get(f"https://api.github.com/repos/{repo}/git/tags/{obj['sha']}", headers=gh_headers)
    if deref.status_code != 200:
        log.error("Failed to dereference annotated tag '%s' in %s: %s", tag, repo, deref.json().get("message"))
        return None
    return deref.json()["object"]["sha"]


def _gh_resolve_tag_or_branch(gh_headers, repo, name):
    """Resolve `name` in repo as a tag first, then as a branch. Returns (sha, kind)
    where kind is "tag" or "branch", or (None, None) if neither resolves (in which case
    the branch-lookup failure has already been logged by _gh_resolve_branch)."""
    tag_sha = _gh_resolve_tag(gh_headers, repo, name)
    if tag_sha:
        return tag_sha, "tag"
    branch_sha = _gh_resolve_branch(gh_headers, repo, name)
    if branch_sha:
        return branch_sha, "branch"
    return None, None


def _gh_create_tag(gh_headers, repo, tag, sha):
    """Create a tag ref; if it already exists, verify it points to the same commit and
    reuse it. Returns SHA or None on failure -- including when a pre-existing tag of the
    same name points somewhere else, which is refused rather than silently reused."""
    url = f"https://api.github.com/repos/{repo}/git/refs"
    resp = http_post(url, headers=gh_headers, json={"ref": f"refs/tags/{tag}", "sha": sha})
    if resp.status_code in (200, 201):
        return resp.json()["object"]["sha"]
    if resp.status_code == 422:
        # Tag already exists -- fetch its SHA via the same safe single-ref lookup used
        # elsewhere (the plural git/refs/:ref endpoint can return a partial-match array
        # instead of one ref, which git/ref/:ref never does).
        resp2 = http_get(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}", headers=gh_headers)
        if resp2.status_code == 200:
            existing_sha = resp2.json()["object"]["sha"]
            if existing_sha == sha:
                log.info("Tag '%s' already exists in %s at %s - reusing", tag, repo, existing_sha)
                return existing_sha
            log.error(
                "Tag '%s' already exists in %s at %s, but this run resolved a different "
                "commit (%s) - refusing to silently reuse a mismatched tag.",
                tag, repo, existing_sha, sha,
            )
            return None
        log.error("Tag '%s' already exists in %s but could not fetch it: %s", tag, repo, resp2.json().get("message"))
        return None
    log.error("Failed to create tag '%s' in %s: %s - %s", tag, repo, resp.status_code, extract_gh_error(resp))
    return None


def _gh_update_submodule(gh_headers, repo, branch, submodule_path, submodule_sha, tag):
    """Pin submodule_path in repo/branch to submodule_sha, fast-forward branch to the
    new commit, and return the new commit SHA."""
    head_sha = _gh_resolve_branch(gh_headers, repo, branch)
    if not head_sha:
        return None

    # Get the root tree SHA of the current HEAD commit
    resp = http_get(f"https://api.github.com/repos/{repo}/git/commits/{head_sha}", headers=gh_headers)
    if resp.status_code != 200:
        log.error("Failed to fetch commit %s in %s: %s", head_sha[:7], repo, resp.json().get("message"))
        return None
    root_tree_sha = resp.json()["tree"]["sha"]

    # Create a new tree from the existing root, overriding only the submodule entry.
    # GitHub resolves all intermediate subtrees automatically when base_tree is provided.
    resp = http_post(
        f"https://api.github.com/repos/{repo}/git/trees",
        headers=gh_headers,
        json={
            "base_tree": root_tree_sha,
            "tree": [{"path": submodule_path, "mode": "160000", "type": "commit", "sha": submodule_sha}],
        },
    )
    if resp.status_code not in (200, 201):
        log.error("Failed to create tree in %s: %s", repo, resp.json().get("message"))
        return None
    new_tree_sha = resp.json()["sha"]

    # Create a commit on the new tree
    resp = http_post(
        f"https://api.github.com/repos/{repo}/git/commits",
        headers=gh_headers,
        json={
            "message": f"chore: update phoenix-data-gateway submodule to {tag}",
            "tree": new_tree_sha,
            "parents": [head_sha],
        },
    )
    if resp.status_code not in (200, 201):
        log.error("Failed to create commit in %s: %s", repo, resp.json().get("message"))
        return None
    new_commit_sha = resp.json()["sha"]

    # Fast-forward the branch to the new commit so it actually reflects the submodule
    # bump, instead of leaving that commit reachable only through the tag we're about
    # to create on it. force=False so this only succeeds as a fast-forward -- if the
    # branch moved concurrently since head_sha was resolved, this fails loudly rather
    # than silently overwriting or orphaning someone else's commit.
    resp = http_patch(
        f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}",
        headers=gh_headers,
        json={"sha": new_commit_sha, "force": False},
    )
    if resp.status_code not in (200, 201):
        log.error(
            "Failed to fast-forward %s '%s' to %s: %s - %s",
            repo, branch, new_commit_sha[:7], resp.status_code, extract_gh_error(resp),
        )
        return None

    return new_commit_sha


def cmd_tag(args):
    name = os.getenv("TAG")
    branch = os.getenv("BRANCH", "main")
    pdg_tag_or_branch = os.getenv("PDG_TAG_OR_BRANCH", branch)
    github_token = os.getenv("GITHUB_TOKEN")
    raw_repos = os.getenv("REPOSITORIES")

    require_env_vars("tag_repository", TAG=name, REPOSITORIES=raw_repos, GITHUB_TOKEN=github_token)

    repositories = [r.strip() for r in raw_repos.split(",") if r.strip()]
    if not repositories:
        log.error("REPOSITORIES is empty.")
        sys.exit(1)

    gh_headers = make_gh_headers(github_token)

    PHOENIX     = "KalderosLLC/phoenix"
    PHOENIX_PDG = "KalderosLLC/phoenix-data-gateway"

    tag_phoenix = PHOENIX in repositories

    # If phoenix is being tagged, phoenix-data-gateway is handled implicitly -
    # remove it from the list so it is not tagged a second time independently.
    remaining = [r for r in repositories if r != PHOENIX and not (r == PHOENIX_PDG and tag_phoenix)]

    if tag_phoenix:
        # 1. Tag phoenix-data-gateway -- PDG_TAG_OR_BRANCH (or BRANCH) is checked as an
        #    existing tag first, then as a branch, so an already-released PDG version can
        #    be pinned directly without needing a same-named branch to exist in PDG.
        pdg_branch_sha, resolved_kind = _gh_resolve_tag_or_branch(gh_headers, PHOENIX_PDG, pdg_tag_or_branch)
        if not pdg_branch_sha:
            sys.exit(1)
        log.info("Resolved %s '%s' (%s) -> %s", PHOENIX_PDG, pdg_tag_or_branch, resolved_kind, pdg_branch_sha)
        pdg_tag_sha = _gh_create_tag(gh_headers, PHOENIX_PDG, name, pdg_branch_sha)
        if not pdg_tag_sha:
            sys.exit(1)
        log.info("Tag '%s' on %s at %s", name, PHOENIX_PDG, pdg_tag_sha)

        # 2. Update submodule pointer in phoenix to the tagged PDG SHA, and fast-forward
        #    branch to that commit so it actually reflects the bump going forward.
        new_sha = _gh_update_submodule(
            gh_headers, PHOENIX, branch,
            "kalderos-edi-functions/phoenix-data-gateway",
            pdg_tag_sha, name,
        )
        if not new_sha:
            sys.exit(1)
        log.info(
            "Submodule pointer in %s updated to %s; branch '%s' fast-forwarded to %s",
            PHOENIX, pdg_tag_sha[:7], branch, new_sha[:7],
        )

        # 3. Tag phoenix at the new submodule-update commit
        if not _gh_create_tag(gh_headers, PHOENIX, name, new_sha):
            sys.exit(1)
        log.info("Tag '%s' created on %s/%s at %s", name, PHOENIX, branch, new_sha)

    for repository in remaining:
        sha = _gh_resolve_branch(gh_headers, repository, branch)
        if not sha:
            sys.exit(1)
        log.info("Resolved %s '%s' -> %s", repository, branch, sha)
        if not _gh_create_tag(gh_headers, repository, name, sha):
            sys.exit(1)
        log.info("Tag '%s' created on %s/%s at %s", name, repository, branch, sha)
