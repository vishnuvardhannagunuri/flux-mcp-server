"""
flux_mcp_server.py
──────────────────
General-purpose Flux CD MCP Server.
Modelled on the official Flux Operator MCP tool set.
Ref: https://fluxoperator.dev/docs/mcp/tools/

Tools:
  REPORTING    → get_flux_instance, get_kubernetes_resources,
                 get_kubernetes_logs, get_kubernetes_metrics,
                 get_kubernetes_api_versions
  RECONCILE    → reconcile_flux_source, reconcile_flux_kustomization,
                 reconcile_flux_helmrelease
  SUSPEND/RESUME → suspend_flux_reconciliation,
                   resume_flux_reconciliation
  CONNECTION   → test_connection

The LLM reads raw cluster data from these tools and performs
root-cause analysis itself — tools are data sources, not answers.

Run:  python flux_mcp_server.py
"""

import logging
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP
import flux_tools

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
# REPORTING — read only, no cluster changes
# ══════════════════════════════════════════════════════════════════

@mcp.tool()
def get_flux_instance() -> dict:
    """
    REPORTING — Flux installation health.

    Returns every controller (source-controller, kustomize-controller,
    helm-controller, etc.) with image version, ready replicas,
    and restart count.

    Per Flux Operator MCP guidelines — call this FIRST when:
    - Nothing in the cluster is reconciling
    - All resources appear stuck
    - 'Is Flux running?', 'What version of Flux is installed?'

    A crashed controller blocks ALL reconciliation for its resource type:
    - source-controller down  → no sources sync
    - kustomize-controller down → no Kustomizations apply
    - helm-controller down    → no HelmReleases deploy
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
    REPORTING — The primary read tool. Retrieves any Flux CRD or
    core Kubernetes resource with full spec, status, conditions,
    and inventory. The LLM reads this raw data to find root cause.

    Supported Flux kinds (case-insensitive):
      kustomization, helmrelease,
      gitrepository, helmrepository, helmchart, ocirepository, bucket,
      alert, provider, receiver,
      imagerepository, imagepolicy, imageupdateautomation

    Supported core kinds:
      pod, deployment, event, configmap, secret, replicaset

    Parameters:
      kind      — resource kind (required)
      name      — specific resource; omit to list all
      namespace — specific namespace; omit for all Flux namespaces
      selector  — label selector e.g. 'app=nginx,env=prod'
      limit     — max results (default 50)

    Per Flux Operator MCP instructions — after getting a resource,
    the LLM should inspect these fields to find root cause:

      spec.sourceRef
        Which GitRepository/HelmRepo this resource reads from.
        If source is failing, this resource will also fail.

      spec.valuesFrom (HelmRelease)
        ConfigMaps/Secrets passed as Helm values.
        If any are missing → HelmRelease will fail.

      spec.substituteFrom (Kustomization)
        ConfigMaps/Secrets used for variable substitution.
        If any are missing → Kustomization will fail.

      spec.dependsOn
        Other Kustomizations that must be Ready first.
        If a dependency is failing → this one is blocked.

      status.inventory (Kustomization)
        Every Kubernetes resource managed by this Kustomization.
        If a managed resource is failing → check its logs.

      metadata.annotations with 'fluxcd.io' labels
        Identifies the parent Kustomization or HelmRelease
        that owns and manages this resource.

    Troubleshooting examples:
      'Show all HelmReleases and their status'
      'Get full detail of ingress-nginx HelmRelease'
      'Which Kustomizations are failing in nwdaf-cd?'
      'Show warning events for cert-manager'
      'List pods in flux-system'
      'Does the secret referenced in valuesFrom exist?'
      'What resources does the flux-system Kustomization manage?'
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

    Returns the last N log lines from a running or crashed container.
    Use previous=True to get logs from the previously crashed instance —
    this is the most important flag for diagnosing CrashLoopBackOff.

    Parameters:
      pod_name       — exact pod name
      pod_namespace  — pod namespace
      container_name — specific container (omit for first container)
      limit          — log lines to return (default 100)
      previous       — get logs from previous crashed instance

    Per Flux Operator MCP instructions, find the pod name first:
      Step 1: get_kubernetes_resources(kind='deployment',
                name='helm-controller', namespace='flux-system')
              → read spec.selector.matchLabels

      Step 2: get_kubernetes_resources(kind='pod',
                selector='app=helm-controller',
                namespace='flux-system')
              → get exact pod name

      Step 3: get_kubernetes_logs(pod_name='helm-controller-abc123',
                pod_namespace='flux-system',
                container_name='manager')
              → read the actual error

    Use for:
      'Why is helm-controller crashing?'
      'Show logs for the failing pod in nwdaf-cd'
      'Get previous container logs after OOMKill'
      'What error is source-controller printing?'
      'Why did the deployment pod fail?'
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

    Use to diagnose resource exhaustion issues:
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
    Never assume the apiVersion of a resource.
    Call this before querying if you are unsure of the version.

    Use for:
      'What Flux CRDs are installed?'
      'What apiVersion does HelmRelease use on this cluster?'
      'Is Flux installed at all?'
    """
    log.info("get_kubernetes_api_versions")
    return flux_tools.get_kubernetes_api_versions()


# ══════════════════════════════════════════════════════════════════
# RECONCILIATION — triggers sync, confirm with user first
# ══════════════════════════════════════════════════════════════════

@mcp.tool()
def reconcile_flux_source(
    kind: str, name: str, namespace: str,
) -> dict:
    """
    RECONCILE — Force immediate re-fetch from the upstream source.

    Supported kinds:
      GitRepository, HelmRepository, OCIRepository, HelmChart, Bucket

    Use after fixing a source issue (auth secret, URL, credentials)
    to trigger re-fetch without waiting for the next interval.

    ALWAYS confirm with user before calling.
    """
    log.info(f"reconcile_flux_source kind={kind} name={name} ns={namespace}")
    return flux_tools.reconcile_flux_source(kind, name, namespace)


@mcp.tool()
def reconcile_flux_kustomization(
    name: str,
    namespace: str,
    with_source: bool = False,
) -> dict:
    """
    RECONCILE — Force immediate re-apply of a Kustomization.

    with_source=True also reconciles the upstream GitRepository first.
    Use this when the source itself may be stale.

    Use after fixing a Kustomization issue (missing secret,
    path error, dependency problem) to apply the fix immediately.

    ALWAYS confirm with user before calling.
    """
    log.info(f"reconcile_flux_kustomization name={name} ns={namespace}")
    return flux_tools.reconcile_flux_kustomization(
        name, namespace, with_source)


@mcp.tool()
def reconcile_flux_helmrelease(
    name: str,
    namespace: str,
    with_source: bool = False,
) -> dict:
    """
    RECONCILE — Force immediate Helm install/upgrade.

    with_source=True also reconciles the HelmRepository/HelmChart first.
    Use this when the chart itself may have been updated.

    Use after fixing a HelmRelease issue (values error, image tag,
    missing secret) to retry the Helm upgrade immediately.

    ALWAYS confirm with user before calling.
    """
    log.info(f"reconcile_flux_helmrelease name={name} ns={namespace}")
    return flux_tools.reconcile_flux_helmrelease(
        name, namespace, with_source)


# ══════════════════════════════════════════════════════════════════
# SUSPEND / RESUME — confirm with user first
# ══════════════════════════════════════════════════════════════════

@mcp.tool()
def suspend_flux_reconciliation(
    kind: str, name: str, namespace: str,
) -> dict:
    """
    SUSPEND — Stop Flux reconciling a resource (spec.suspend=true).

    Use to pause a broken resource while manually fixing the root cause.
    Flux will not overwrite manual changes while suspended.

    Supported kinds: any Flux CRD kind
      Kustomization, HelmRelease, GitRepository,
      HelmRepository, OCIRepository, HelmChart, etc.

    Standard fix workflow:
      1. suspend           → Flux stops touching it
      2. Fix root cause    → edit secret, fix values, update image
      3. resume            → Flux reconciles again
      4. reconcile         → trigger immediately if needed

    ALWAYS confirm with user before calling.
    """
    log.info(f"suspend kind={kind} name={name} ns={namespace}")
    return flux_tools.suspend_flux_reconciliation(kind, name, namespace)


@mcp.tool()
def resume_flux_reconciliation(
    kind: str, name: str, namespace: str,
) -> dict:
    """
    RESUME — Re-enable Flux reconciliation (spec.suspend=false).

    Use after fixing the root cause on a suspended resource.
    Flux will reconcile on the next interval.
    Call the matching reconcile tool after this to trigger immediately.

    ALWAYS confirm with user before calling.
    """
    log.info(f"resume kind={kind} name={name} ns={namespace}")
    return flux_tools.resume_flux_reconciliation(kind, name, namespace)


# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("Flux MCP Server starting — stdio transport")
    log.info(f"Namespaces: {flux_tools.NAMESPACES}")
    mcp.run()
