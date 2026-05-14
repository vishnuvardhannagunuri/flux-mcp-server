"""
flux_mcp_server.py
──────────────────
Read-only Flux CD MCP Server for troubleshooting and diagnosis.
No write, patch, delete, or reconcile operations.

Tool categories:
  CONNECTION   — test_connection
  REPORTING    — get_flux_instance, get_kubernetes_resources,
                 get_kubernetes_logs, get_kubernetes_metrics,
                 get_kubernetes_api_versions
  GITLAB       — get_gitlab_file, list_gitlab_files,
                 get_gitlab_commits, get_gitlab_commit_diff,
                 compare_gitlab_to_cluster

Modelled on: https://fluxoperator.dev/docs/mcp/tools/#reporting-tools

Run:  python flux_mcp_server.py
"""

import logging
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP
import flux_tools
import gitlab_tools

logging.basicConfig(
    stream=sys.stderr, level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("flux-mcp")

mcp = FastMCP("flux-mcp-server")


# ══════════════════════════════════════════════════════════════════
# CONNECTION
# ══════════════════════════════════════════════════════════════════

@mcp.tool()
def test_connection() -> dict:
    """
    Test connectivity to the OpenShift cluster.
    Always run this first to confirm the agent can reach the cluster.
    """
    log.info("test_connection")
    return flux_tools.test_connection()


# ══════════════════════════════════════════════════════════════════
# REPORTING — cluster state, read-only
# ══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_flux_instance() -> dict:
    """
    REPORTING — Flux controller health.

    Returns every controller (source-controller, kustomize-controller,
    helm-controller, etc.) with image version, ready replicas,
    and restart count.

    Call this FIRST when:
    - Nothing in the cluster is reconciling
    - All resources appear stuck
    - 'Is Flux running?', 'What version is installed?'

    If a controller is down, ALL resources of that type are frozen:
    - source-controller down    → no sources sync
    - kustomize-controller down → no Kustomizations apply
    - helm-controller down      → no HelmReleases deploy
    """
    log.info("get_flux_instance")
    return flux_tools.get_flux_instance()


@mcp.tool()
def get_kubernetes_resources(
    kind: str,
    name: Optional[str]      = None,
    namespace: Optional[str] = None,
    selector: Optional[str]  = None,
    limit: int               = 50,
) -> dict:
    """
    REPORTING — Retrieve any Flux CRD or core Kubernetes resource.
    Returns full spec, status, conditions, and inventory.
    The LLM reads this raw data to find the root cause.

    Supported Flux kinds (case-insensitive):
      kustomization, helmrelease,
      gitrepository, helmrepository, helmchart, ocirepository, bucket,
      alert, provider, receiver,
      imagerepository, imagepolicy, imageupdateautomation

    Supported core kinds:
      pod, deployment, event, configmap, secret

    Parameters:
      kind      — resource kind (required)
      name      — specific resource; omit to list all
      namespace — specific namespace; omit for all Flux namespaces
      selector  — label selector e.g. 'app=nginx'
      limit     — max results (default 50)

    After getting a resource the LLM should inspect:

      spec.sourceRef
        Which source this resource reads from.
        If the source is failing, this resource will also fail.

      spec.valuesFrom  (HelmRelease)
        ConfigMaps/Secrets used as Helm values.
        If any are missing the HelmRelease will fail.

      spec.substituteFrom  (Kustomization)
        ConfigMaps/Secrets for variable substitution.
        If any are missing the Kustomization will fail.

      spec.dependsOn  (Kustomization)
        Other Kustomizations that must be Ready first.
        If a dependency is failing this one is blocked.

      status.inventory  (Kustomization)
        Every resource managed by this Kustomization.
        If a managed resource is failing check its logs.

      metadata.annotations with fluxcd.io labels
        Identifies the parent Kustomization or HelmRelease.

    Use for:
      'Show all HelmReleases and their status'
      'Which Kustomizations are failing in nwdaf-cd?'
      'Get full detail of ingress-nginx HelmRelease'
      'Show warning events for cert-manager'
      'List pods in flux-system'
      'Does the secret referenced in valuesFrom exist?'
    """
    log.info(f"get_kubernetes_resources kind={kind} name={name} "
             f"ns={namespace} selector={selector}")
    return flux_tools.get_kubernetes_resources(
        kind=kind, name=name, namespace=namespace,
        selector=selector, limit=limit)


@mcp.tool()
def get_kubernetes_logs(
    pod_name: str,
    pod_namespace: str,
    container_name: Optional[str] = None,
    limit: int                    = 100,
    previous: bool                = False,
) -> dict:
    """
    REPORTING — Get logs from a pod container.

    Returns the last N log lines. Use previous=True to get logs
    from the previously crashed container — essential for diagnosing
    CrashLoopBackOff and OOMKilled containers.

    Parameters:
      pod_name       — exact pod name
      pod_namespace  — namespace the pod is in
      container_name — specific container (omit for first container)
      limit          — lines to return (default 100)
      previous       — get logs from previous crashed instance

    Per Flux Operator MCP guidelines, find the pod name first:
      1. get_kubernetes_resources(kind='deployment',
                                  name='helm-controller',
                                  namespace='flux-system')
         → find spec.selector.matchLabels
      2. get_kubernetes_resources(kind='pod',
                                  selector='app=helm-controller',
                                  namespace='flux-system')
         → find exact pod name
      3. get_kubernetes_logs(pod_name='helm-controller-xxx',
                              pod_namespace='flux-system',
                              container_name='manager')

    Use for:
      'Why is helm-controller crashing?'
      'Show logs for the failing pod'
      'Get previous container logs after OOMKill'
      'What error is source-controller printing?'
    """
    log.info(f"get_kubernetes_logs pod={pod_name} ns={pod_namespace} "
             f"previous={previous}")
    return flux_tools.get_kubernetes_logs(
        pod_name=pod_name, pod_namespace=pod_namespace,
        container_name=container_name, limit=limit, previous=previous)


@mcp.tool()
def get_kubernetes_metrics(
    pod_namespace: str,
    pod_name: Optional[str]     = None,
    pod_selector: Optional[str] = None,
    limit: int                  = 20,
) -> dict:
    """
    REPORTING — CPU and Memory usage for pods.
    Requires metrics-server installed in the cluster.

    Use to diagnose resource exhaustion:
      'Is helm-controller being OOMKilled?'
      'Is source-controller CPU throttled?'
      'Show resource usage in nwdaf-cd'
    """
    log.info(f"get_kubernetes_metrics ns={pod_namespace} pod={pod_name}")
    return flux_tools.get_kubernetes_metrics(
        pod_namespace=pod_namespace, pod_name=pod_name,
        pod_selector=pod_selector, limit=limit)


@mcp.tool()
def get_kubernetes_api_versions() -> dict:
    """
    REPORTING — List all Flux CRDs with their preferred apiVersion.

    Per Flux Operator MCP guidelines:
    Never assume the apiVersion. Call this first when unsure.

    Use for:
      'What Flux CRDs are installed?'
      'What apiVersion does HelmRelease use?'
      'Is Flux installed at all?'
    """
    log.info("get_kubernetes_api_versions")
    return flux_tools.get_kubernetes_api_versions()


# ══════════════════════════════════════════════════════════════════
# GITLAB — repository read-only
# ══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_gitlab_file(
    file_path: str,
    ref: str = "main",
) -> dict:
    """
    GITLAB — Fetch a single config file from the GitLab repository.

    file_path — path inside the repo
                e.g. 'clusters/nwdaf-cd/helmrelease.yaml'
    ref       — branch, tag, or commit SHA (default: main)

    Returns the raw file content so the LLM can compare it
    with the live cluster state from get_kubernetes_resources.

    Use for:
      'Show me the HelmRelease config in Git'
      'What image tag is defined in the Git manifest?'
      'What values does the kustomization.yaml define?'
      'Compare the Git config with what is running on the cluster'
    """
    log.info(f"get_gitlab_file path={file_path} ref={ref}")
    return gitlab_tools.get_gitlab_file(file_path, ref)


@mcp.tool()
def list_gitlab_files(
    path: str       = "",
    ref: str        = "main",
    recursive: bool = False,
) -> dict:
    """
    GITLAB — List files and folders in the GitLab repository.

    path      — folder path inside the repo (empty = root)
    ref       — branch, tag, or commit SHA (default: main)
    recursive — list all files recursively (default: false)

    Use to discover what config files exist:
      'What Flux config files are in the clusters/ folder?'
      'List all HelmRelease YAML files in the repo'
      'Show me the folder structure for nwdaf-cd'
    """
    log.info(f"list_gitlab_files path={path} ref={ref}")
    return gitlab_tools.list_gitlab_files(path, ref, recursive)


@mcp.tool()
def get_gitlab_commits(
    ref: str   = "main",
    path: str  = "",
    limit: int = 10,
) -> dict:
    """
    GITLAB — Get recent commits from the repository.

    ref   — branch or tag (default: main)
    path  — filter commits that touched a specific file or folder
    limit — number of commits (default: 10)

    Use to correlate recent changes with failures:
      'What was pushed to main recently?'
      'Who changed the HelmRelease config?'
      'Show commits that touched nwdaf-cd before it broke'
      'What changed before the deployment started failing?'
    """
    log.info(f"get_gitlab_commits ref={ref} path={path}")
    return gitlab_tools.get_gitlab_commits(ref, path, limit)


@mcp.tool()
def get_gitlab_commit_diff(sha: str) -> dict:
    """
    GITLAB — Get the exact file changes in a specific commit.

    sha — full or short commit SHA

    Use to see precisely what changed:
      'What files did this commit change?'
      'Show me the diff for the last deployment commit'
      'What exactly changed in commit abc1234?'
    """
    log.info(f"get_gitlab_commit_diff sha={sha}")
    return gitlab_tools.get_gitlab_commit_diff(sha)


@mcp.tool()
def compare_gitlab_to_cluster(
    file_path: str,
    ref: str = "main",
) -> dict:
    """
    GITLAB + CLUSTER — Fetch a config file from GitLab and prompt
    the LLM to compare it with the live cluster state.

    This is the key tool for diagnosing:
    'I pushed to GitLab but changes are not on the cluster'

    It fetches the Git version of the config and tells the LLM
    to call get_kubernetes_resources for the same resource so
    it can compare spec fields and find the difference.

    Use for:
      'Is the cluster running what is in Git?'
      'Did my push actually get applied to the cluster?'
      'Compare the HelmRelease in Git with the cluster'
      'Why is the cluster not matching what I pushed?'
    """
    log.info(f"compare_gitlab_to_cluster path={file_path} ref={ref}")
    return gitlab_tools.compare_gitlab_to_cluster(file_path, ref)


# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("Flux MCP Server starting (READ-ONLY) — stdio transport")
    log.info(f"Cluster namespaces: {flux_tools.NAMESPACES}")
    log.info("No write operations available — diagnosis only")
    mcp.run()
