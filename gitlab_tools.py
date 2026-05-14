"""
gitlab_tools.py
───────────────
Read-only GitLab tools — fetch config files from your repository.

Uses the GitLab REST API with a Personal Access Token (read-only scope).
The LLM uses these to compare what is in Git vs what is on the cluster.

Required env vars:
  GITLAB_URL    — e.g. https://gitlab.mycompany.com
  GITLAB_TOKEN  — Personal Access Token with read_repository scope
  GITLAB_PROJECT_ID — numeric project ID or 'namespace/project'

All operations are GET requests only — nothing is written to GitLab.
"""

import os
import urllib.request
import urllib.parse
import urllib.error
import json
from typing import Optional


def _gitlab_get(path: str) -> dict:
    """Make a GET request to the GitLab API."""
    base    = os.getenv("GITLAB_URL", "").rstrip("/")
    token   = os.getenv("GITLAB_TOKEN", "")
    project = os.getenv("GITLAB_PROJECT_ID", "")

    if not base or not token or not project:
        return {
            "error": "Missing GitLab config. Set env vars: "
                     "GITLAB_URL, GITLAB_TOKEN, GITLAB_PROJECT_ID"
        }

    encoded_project = urllib.parse.quote(str(project), safe="")
    url = f"{base}/api/v4/projects/{encoded_project}/{path}"

    req = urllib.request.Request(
        url, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════
# GitLab tools — all read-only
# ══════════════════════════════════════════════════════════════════

def get_gitlab_file(
    file_path: str,
    ref: str = "main",
) -> dict:
    """
    READ-ONLY — Fetch a single file from the GitLab repository.

    file_path — path inside the repo e.g. 'clusters/nwdaf/helmrelease.yaml'
    ref       — branch, tag, or commit SHA (default: main)

    Returns the raw file content as a string so the LLM can
    compare it with the live cluster state.

    Use for:
      'Show me the HelmRelease config in Git'
      'What values does the kustomization.yaml define?'
      'Compare the Git config with the cluster state'
      'What image tag is defined in the deployment manifest?'
    """
    encoded = urllib.parse.quote(file_path, safe="")
    result  = _gitlab_get(f"repository/files/{encoded}/raw?ref={ref}")

    if isinstance(result, dict) and "error" in result:
        return result

    # result is raw text when successful
    return {
        "file":    file_path,
        "ref":     ref,
        "content": result if isinstance(result, str) else str(result),
    }


def list_gitlab_files(
    path: str        = "",
    ref: str         = "main",
    recursive: bool  = False,
) -> dict:
    """
    READ-ONLY — List files and folders in a GitLab repository path.

    path      — folder path inside the repo (empty = root)
    ref       — branch, tag, or commit SHA (default: main)
    recursive — list all files recursively (default: false)

    Use to discover what config files exist in the repository:
      'What Flux config files are in the clusters/ folder?'
      'List all HelmRelease files in the repo'
      'Show me the folder structure for nwdaf-cd configs'
    """
    params = f"path={urllib.parse.quote(path)}&ref={ref}&per_page=50"
    if recursive:
        params += "&recursive=true"
    result = _gitlab_get(f"repository/tree?{params}")

    if isinstance(result, dict) and "error" in result:
        return result

    items = result if isinstance(result, list) else []
    return {
        "path":      path or "/",
        "ref":       ref,
        "total":     len(items),
        "items": [{
            "name": i.get("name", ""),
            "path": i.get("path", ""),
            "type": i.get("type", ""),  # blob = file, tree = folder
        } for i in items],
    }


def get_gitlab_commits(
    ref: str  = "main",
    path: str = "",
    limit: int = 10,
) -> dict:
    """
    READ-ONLY — Get recent commits from the GitLab repository.

    ref   — branch or tag (default: main)
    path  — filter commits that touched a specific file/folder
    limit — number of commits to return (default: 10)

    Use to find what changed recently and correlate with failures:
      'What was the last commit pushed to main?'
      'Who changed the HelmRelease config recently?'
      'Show commits that touched the nwdaf-cd folder'
      'What changed before the deployment started failing?'
    """
    params = f"ref_name={ref}&per_page={limit}"
    if path:
        params += f"&path={urllib.parse.quote(path)}"
    result = _gitlab_get(f"repository/commits?{params}")

    if isinstance(result, dict) and "error" in result:
        return result

    commits = result if isinstance(result, list) else []
    return {
        "ref":     ref,
        "path":    path or "all files",
        "total":   len(commits),
        "commits": [{
            "sha":       c.get("id",              ""),
            "shortSha":  c.get("short_id",        ""),
            "message":   c.get("title",           ""),
            "author":    c.get("author_name",     ""),
            "email":     c.get("author_email",    ""),
            "date":      c.get("committed_date",  ""),
            "webUrl":    c.get("web_url",         ""),
        } for c in commits],
    }


def get_gitlab_commit_diff(sha: str) -> dict:
    """
    READ-ONLY — Get the file changes introduced by a specific commit.

    sha — the full or short commit SHA

    Use to see exactly what changed in a specific commit:
      'What files did this commit change?'
      'Show me the diff for the last deployment commit'
      'What exactly changed in commit abc1234?'
    """
    result = _gitlab_get(f"repository/commits/{sha}/diff")

    if isinstance(result, dict) and "error" in result:
        return result

    diffs = result if isinstance(result, list) else []
    return {
        "sha":    sha,
        "total":  len(diffs),
        "changes": [{
            "file":      d.get("new_path",    ""),
            "oldFile":   d.get("old_path",    ""),
            "newFile":   d.get("new_file",    False),
            "deleted":   d.get("deleted_file",False),
            "renamed":   d.get("renamed_file",False),
            "diff":      d.get("diff",        "")[:2000],  # cap size
        } for d in diffs],
    }


def compare_gitlab_to_cluster(
    file_path: str,
    ref: str = "main",
) -> dict:
    """
    READ-ONLY — Fetch a config file from GitLab and return it
    alongside a reminder to compare with cluster state.

    This is a convenience tool that fetches the Git config so
    the LLM can compare it with a get_kubernetes_resources result
    in the same conversation.

    Use for:
      'Compare the HelmRelease in Git with what is on the cluster'
      'Is the cluster running what is in Git?'
      'Did my push actually get applied?'
    """
    file_result = get_gitlab_file(file_path, ref)
    if "error" in file_result:
        return file_result

    return {
        "source":       "gitlab",
        "file":         file_path,
        "ref":          ref,
        "content":      file_result["content"],
        "nextStep": (
            "Use get_kubernetes_resources to fetch the same resource "
            "from the cluster, then compare spec fields to find "
            "differences between Git and cluster state."
        ),
    }
