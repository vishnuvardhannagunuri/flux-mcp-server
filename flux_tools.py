"""
flux_tools.py
─────────────
General-purpose Flux CD troubleshooting tools.
Modelled on the official Flux Operator MCP tool categories:

  REPORTING    — get_flux_instance, get_kubernetes_resources,
                 get_kubernetes_logs, get_kubernetes_metrics,
                 get_kubernetes_api_versions
  RECONCILE    — reconcile_flux_source, reconcile_flux_kustomization,
                 reconcile_flux_helmrelease
  SUSPEND/RESUME — suspend_flux_reconciliation,
                   resume_flux_reconciliation

The LLM reads raw data returned by these tools and
performs the diagnosis itself — tools are data sources,
not pre-canned answers.

Ref: https://fluxoperator.dev/docs/mcp/tools/
     https://fluxoperator.dev/docs/mcp/instructions/
"""

import datetime
from typing import Optional
from kubernetes.client.rest import ApiException
from kube_client import get_client, test_connection   # noqa: F401  (re-exported)

# ── namespaces this agent covers ─────────────────────────────────
NAMESPACES = ["flux-system", "nwdaf-cd"]

# ── Flux API groups + versions + plurals ─────────────────────────
KIND_MAP = {
    # appliers
    "kustomization":        ("kustomize.toolkit.fluxcd.io",     "v1",      "kustomizations"),
    "helmrelease":          ("helm.toolkit.fluxcd.io",           "v2",      "helmreleases"),
    # sources
    "gitrepository":        ("source.toolkit.fluxcd.io",         "v1",      "gitrepositories"),
    "helmrepository":       ("source.toolkit.fluxcd.io",         "v1",      "helmrepositories"),
    "helmchart":            ("source.toolkit.fluxcd.io",         "v1",      "helmcharts"),
    "ocirepository":        ("source.toolkit.fluxcd.io",         "v1",      "ocirepositories"),
    "bucket":               ("source.toolkit.fluxcd.io",         "v1",      "buckets"),
    # notification
    "alert":                ("notification.toolkit.fluxcd.io",   "v1beta3", "alerts"),
    "provider":             ("notification.toolkit.fluxcd.io",   "v1beta3", "providers"),
    "receiver":             ("notification.toolkit.fluxcd.io",   "v1",      "receivers"),
    # image automation
    "imagerepository":      ("image.toolkit.fluxcd.io",          "v1beta2", "imagerepositories"),
    "imagepolicy":          ("image.toolkit.fluxcd.io",          "v1beta2", "imagepolicies"),
    "imageupdateautomation":("image.toolkit.fluxcd.io",          "v1beta1", "imageupdateautomations"),
}

FLUX_CONTROLLERS = [
    "source-controller",
    "kustomize-controller",
    "helm-controller",
    "notification-controller",
    "image-reflector-controller",
    "image-automation-controller",
]


# ══════════════════════════════════════════════════════════════════
# internal helpers
# ══════════════════════════════════════════════════════════════════

def _ready(conditions: list) -> dict:
    for c in conditions:
        if c.get("type") == "Ready":
            return {
                "status":             c.get("status", "Unknown"),
                "reason":             c.get("reason", ""),
                "message":            c.get("message", ""),
                "lastTransitionTime": c.get("lastTransitionTime", ""),
            }
    return {"status": "Unknown", "reason": "", "message": "",
            "lastTransitionTime": ""}


def _summarise(item: dict) -> dict:
    """Compact summary of a Flux CRD item for listing."""
    meta       = item.get("metadata", {})
    spec       = item.get("spec",     {})
    status     = item.get("status",   {})
    r          = _ready(status.get("conditions", []))
    return {
        "name":               meta.get("name",      ""),
        "namespace":          meta.get("namespace", ""),
        "kind":               item.get("kind",       ""),
        "ready":              r["status"],
        "reason":             r["reason"],
        "message":            r["message"],
        "lastTransitionTime": r["lastTransitionTime"],
        "suspended":          spec.get("suspend", False),
        "revision":           status.get(
            "lastAppliedRevision",
            status.get("artifact", {}).get("revision", ""),
        ),
        # keep full spec + status so LLM can inspect
        # sourceRef, valuesFrom, substituteFrom, inventory, etc.
        "spec":   spec,
        "status": status,
    }


def _annotate(group, version, plural, name, namespace) -> dict:
    """Patch the reconcile annotation to trigger immediate sync."""
    api, _, _ = get_client()
    now = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        api.patch_namespaced_custom_object(
            group, version, namespace, plural, name,
            body={"metadata": {"annotations": {
                "reconcile.fluxcd.io/requestedAt": now,
            }}},
        )
        return {
            "triggered": True,
            "name": name, "namespace": namespace, "at": now,
            "verify": (
                f"kubectl get {plural} {name} -n {namespace} "
                f"-o jsonpath='{{.status.conditions}}'"
            ),
        }
    except ApiException as e:
        return {"triggered": False, "error": e.reason, "code": e.status}


# ══════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════

def get_flux_instance() -> dict:
    """
    REPORTING — Flux installation health.

    Returns every controller with image version, desired/ready
    replicas, and restart count.

    Per Flux Operator MCP instructions:
    Call this FIRST when nothing reconciles or all resources
    appear stuck. A crashed controller blocks ALL reconciliation
    for that resource type.
    """
    _, core, apps = get_client()
    results = []

    for name in FLUX_CONTROLLERS:
        info = {"name": name, "status": "not-installed",
                "ready": False, "restarts": 0, "image": ""}
        try:
            d       = apps.read_namespaced_deployment(name, "flux-system")
            desired = d.spec.replicas or 1
            ready   = d.status.ready_replicas or 0
            image   = (d.spec.template.spec.containers[0].image
                       if d.spec.template.spec.containers else "")
            info.update({
                "status":  "healthy" if ready >= desired else "degraded",
                "ready":   ready >= desired,
                "desired": desired,
                "running": ready,
                "image":   image,
            })
        except ApiException:
            pass

        try:
            pods = core.list_namespaced_pod(
                "flux-system", label_selector=f"app={name}")
            for p in pods.items:
                for cs in (p.status.container_statuses or []):
                    info["restarts"] += cs.restart_count
        except Exception:
            pass

        results.append(info)

    unhealthy = [c["name"] for c in results if not c["ready"]]
    return {
        "controllers": results,
        "summary": {
            "total":     len(results),
            "healthy":   len(results) - len(unhealthy),
            "unhealthy": len(unhealthy),
            "allHealthy":len(unhealthy) == 0,
            "unhealthyControllers": unhealthy,
        },
    }


def get_kubernetes_resources(
    kind: str,
    name: Optional[str]      = None,
    namespace: Optional[str] = None,
    selector: Optional[str]  = None,
    limit: int               = 50,
) -> dict:
    """
    REPORTING — Retrieve any Flux CRD or core Kubernetes resource.

    Returns full spec, status conditions, events, and inventory
    so the LLM can analyse root cause without extra calls.

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
      namespace — specific namespace; omit to search all Flux namespaces
      selector  — label selector e.g. 'app=nginx'
      limit     — max results (default 50)

    Per Flux Operator MCP instructions, the LLM should inspect:
      spec.sourceRef       — what source this resource reads from
      spec.valuesFrom      — ConfigMaps/Secrets referenced by HelmRelease
      spec.substituteFrom  — ConfigMaps/Secrets referenced by Kustomization
      spec.dependsOn       — dependency ordering
      status.inventory     — resources managed by this Kustomization
      metadata.annotations — fluxcd.io labels to find the owner
    """
    api, core, apps = get_client()
    k = kind.lower()

    # ── Flux CRDs ─────────────────────────────────────────────────
    if k in KIND_MAP:
        group, version, plural = KIND_MAP[k]
        namespaces = [namespace] if namespace else NAMESPACES
        resources  = []

        for ns in namespaces:
            try:
                if name:
                    items = [api.get_namespaced_custom_object(
                        group, version, ns, plural, name)]
                else:
                    raw   = api.list_namespaced_custom_object(
                        group, version, ns, plural,
                        label_selector=selector or "", limit=limit)
                    items = raw.get("items", [])

                for item in items:
                    resources.append(_summarise(item))

            except ApiException as e:
                if e.status == 404 and name:
                    return {"error": f"{kind} '{name}' not found in '{ns}'",
                            "tip":   f"Searched namespaces: {NAMESPACES}"}
                if e.status not in [403, 404]:
                    resources.append({"namespace": ns, "error": str(e)})

        failing   = [r for r in resources
                     if r.get("ready") != "True"
                     and not r.get("suspended")
                     and "error" not in r]
        suspended = [r for r in resources if r.get("suspended")]

        return {
            "kind":        kind,
            "total":       len(resources),
            "failing":     len(failing),
            "suspended":   len(suspended),
            "resources":   resources,
            "failingList": failing,
        }

    # ── Core Kubernetes resources ──────────────────────────────────
    ns = namespace or "flux-system"

    if k == "pod":
        try:
            if name:
                pods = [core.read_namespaced_pod(name, ns)]
            elif selector:
                pods = core.list_namespaced_pod(
                    ns, label_selector=selector, limit=limit).items
            else:
                pods = core.list_namespaced_pod(ns, limit=limit).items

            return {"kind": "Pod", "namespace": ns, "pods": [{
                "name":     p.metadata.name,
                "phase":    p.status.phase,
                "ready":    all(cs.ready for cs in
                                (p.status.container_statuses or [])),
                "restarts": sum(cs.restart_count for cs in
                                (p.status.container_statuses or [])),
                "containers": [{
                    "name":    cs.name,
                    "image":   cs.image,
                    "ready":   cs.ready,
                    "restarts":cs.restart_count,
                    "state":   str(cs.state),
                } for cs in (p.status.container_statuses or [])],
            } for p in pods]}
        except ApiException as e:
            return {"error": str(e)}

    if k == "deployment":
        try:
            if name:
                deps = [apps.read_namespaced_deployment(name, ns)]
            else:
                deps = apps.list_namespaced_deployment(
                    ns, limit=limit).items
            return {"kind": "Deployment", "namespace": ns,
                    "deployments": [{
                        "name":        d.metadata.name,
                        "desired":     d.spec.replicas,
                        "ready":       d.status.ready_replicas or 0,
                        "available":   d.status.available_replicas or 0,
                        "matchLabels": (d.spec.selector.match_labels or {}),
                    } for d in deps]}
        except ApiException as e:
            return {"error": str(e)}

    if k == "event":
        try:
            field = (f"involvedObject.name={name}" if name
                     else "type!=Normal")
            evs = core.list_namespaced_event(
                ns, field_selector=field, limit=limit)
            return {"kind": "Event", "namespace": ns,
                    "filter": name or "warnings/errors",
                    "events": [{
                        "type":    e.type,
                        "reason":  e.reason,
                        "object":  (f"{e.involved_object.kind}/"
                                    f"{e.involved_object.name}"),
                        "message": e.message,
                        "count":   e.count,
                        "lastSeen":str(e.last_timestamp),
                    } for e in sorted(
                        evs.items,
                        key=lambda x: (
                            x.last_timestamp or
                            datetime.datetime.min.replace(
                                tzinfo=datetime.timezone.utc)
                        ),
                        reverse=True,
                    )]}
        except ApiException as e:
            return {"error": str(e)}

    if k == "configmap":
        try:
            if name:
                cm = core.read_namespaced_config_map(name, ns)
                return {"kind": "ConfigMap", "name": name,
                        "namespace": ns, "data": cm.data or {}}
            items = core.list_namespaced_config_map(ns, limit=limit).items
            return {"kind": "ConfigMap", "namespace": ns,
                    "items": [i.metadata.name for i in items]}
        except ApiException as e:
            return {"error": str(e)}

    if k == "secret":
        # Never return secret values — names only
        try:
            items = core.list_namespaced_secret(ns, limit=limit).items
            return {"kind": "Secret", "namespace": ns,
                    "note": "Values not returned for security",
                    "secrets": [{"name": s.metadata.name, "type": s.type}
                                for s in items]}
        except ApiException as e:
            return {"error": str(e)}

    if k == "replicaset":
        try:
            _, _, apps2 = get_client()
            if name:
                rs = [apps2.read_namespaced_replica_set(name, ns)]
            else:
                rs = apps2.list_namespaced_replica_set(
                    ns, label_selector=selector or "", limit=limit).items
            return {"kind": "ReplicaSet", "namespace": ns,
                    "replicasets": [{
                        "name":     r.metadata.name,
                        "desired":  r.spec.replicas or 0,
                        "ready":    r.status.ready_replicas or 0,
                        "labels":   r.metadata.labels or {},
                    } for r in rs]}
        except ApiException as e:
            return {"error": str(e)}

    return {
        "error":     f"kind '{kind}' not supported",
        "supported": list(KIND_MAP.keys()) + [
            "pod", "deployment", "event", "configmap", "secret", "replicaset"
        ],
    }


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
    from a crashed/restarted container — essential for diagnosing
    CrashLoopBackOff and OOMKilled.

    Per Flux Operator MCP instructions, to get controller logs:
      1. get_kubernetes_resources(kind='deployment',
                                  name='helm-controller',
                                  namespace='flux-system')
         → find matchLabels from the deployment spec
      2. get_kubernetes_resources(kind='pod',
                                  selector='app=helm-controller',
                                  namespace='flux-system')
         → find the exact pod name
      3. get_kubernetes_logs(pod_name='helm-controller-xxx',
                             pod_namespace='flux-system',
                             container_name='manager')
         → read the actual error
    """
    _, core, _ = get_client()
    try:
        pod    = core.read_namespaced_pod(pod_name, pod_namespace)
        target = container_name or pod.spec.containers[0].name
        logs   = core.read_namespaced_pod_log(
            name=pod_name, namespace=pod_namespace,
            container=target, tail_lines=limit, previous=previous)
        return {
            "pod":          pod_name,
            "namespace":    pod_namespace,
            "container":    target,
            "previous":     previous,
            "linesReturned":len(logs.splitlines()),
            "logs":         logs,
        }
    except ApiException as e:
        return {"error": str(e),
                "tip": ("If previous=True and not found, the container "
                        "has not restarted yet in this session.")}


def get_kubernetes_metrics(
    pod_namespace: str,
    pod_name: Optional[str]     = None,
    pod_selector: Optional[str] = None,
    limit: int                  = 20,
) -> dict:
    """
    REPORTING — CPU and Memory usage for pods.
    Requires metrics-server installed in the cluster.

    Use to check if a controller is OOMKilling or CPU-throttled.
    """
    api, _, _ = get_client()
    try:
        if pod_name:
            items = [api.get_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1",
                pod_namespace, "pods", pod_name)]
        else:
            raw   = api.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", pod_namespace, "pods",
                label_selector=pod_selector or "", limit=limit)
            items = raw.get("items", [])

        return {
            "namespace": pod_namespace,
            "total":     len(items),
            "metrics": [{
                "pod": pm["metadata"]["name"],
                "containers": [{
                    "name":   c["name"],
                    "cpu":    c["usage"].get("cpu",    ""),
                    "memory": c["usage"].get("memory", ""),
                } for c in pm.get("containers", [])],
            } for pm in items],
        }
    except ApiException as e:
        if e.status == 404:
            return {"error": "metrics-server not installed",
                    "install": (
                        "kubectl apply -f https://github.com/kubernetes-sigs"
                        "/metrics-server/releases/latest/download/components.yaml"
                    )}
        return {"error": str(e)}


def get_kubernetes_api_versions() -> dict:
    """
    REPORTING — List all Flux CRDs installed with their preferred apiVersion.

    Per Flux Operator MCP instructions:
    Never assume the apiVersion of a resource.
    Call this first when unsure which version to use.
    """
    import kubernetes
    try:
        crds_api = kubernetes.client.ApiextensionsV1Api()
        crd_list = crds_api.list_custom_resource_definition()
        flux = {}
        for crd in crd_list.items:
            if "fluxcd.io" not in crd.spec.group:
                continue
            versions  = crd.spec.versions or []
            preferred = (next((v.name for v in versions if v.storage), None)
                         or (versions[0].name if versions else "v1"))
            flux[crd.spec.names.kind] = {
                "apiVersion": f"{crd.spec.group}/{preferred}",
                "plural":     crd.spec.names.plural,
            }
        return {"fluxCRDs": flux, "total": len(flux)}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════
# RECONCILIATION
# ══════════════════════════════════════════════════════════════════

def reconcile_flux_source(kind: str, name: str, namespace: str) -> dict:
    """
    RECONCILE — Trigger immediate re-fetch from the upstream source.
    Supports: GitRepository, HelmRepository, OCIRepository,
              HelmChart, Bucket.
    """
    k = kind.lower()
    if k not in KIND_MAP:
        return {"error": f"Unknown source kind '{kind}'"}
    g, v, p  = KIND_MAP[k]
    result   = _annotate(g, v, p, name, namespace)
    result["message"] = (
        f"Source {kind}/{name} triggered. "
        f"Flux will re-fetch from upstream. ~30s to complete.")
    return result


def reconcile_flux_kustomization(
    name: str, namespace: str, with_source: bool = False
) -> dict:
    """
    RECONCILE — Trigger immediate re-apply of a Kustomization.
    with_source=True also reconciles the upstream source first.
    """
    g, v, p    = KIND_MAP["kustomization"]
    src_result = None

    if with_source:
        api, _, _ = get_client()
        try:
            kust    = api.get_namespaced_custom_object(
                g, v, namespace, p, name)
            src_ref = kust.get("spec", {}).get("sourceRef", {})
            sk, sn  = src_ref.get("kind", "").lower(), src_ref.get("name", "")
            sns     = src_ref.get("namespace", namespace)
            if sk in KIND_MAP and sn:
                sg, sv, sp = KIND_MAP[sk]
                src_result = _annotate(sg, sv, sp, sn, sns)
        except Exception as e:
            src_result = {"error": str(e)}

    result = _annotate(g, v, p, name, namespace)
    result.update({
        "withSource":   with_source,
        "sourceResult": src_result,
        "message": (
            f"Kustomization/{name} triggered"
            + (" with source" if with_source else "")
            + ". Flux will re-apply manifests. ~30s to complete."),
    })
    return result


def reconcile_flux_helmrelease(
    name: str, namespace: str, with_source: bool = False
) -> dict:
    """
    RECONCILE — Trigger immediate Helm install/upgrade.
    with_source=True also reconciles the HelmRepository/HelmChart first.
    """
    g, v, p    = KIND_MAP["helmrelease"]
    src_result = None

    if with_source:
        api, _, _ = get_client()
        try:
            hr      = api.get_namespaced_custom_object(
                g, v, namespace, p, name)
            # support both chartRef and spec.chart.spec.sourceRef
            ref = (hr.get("spec", {}).get("chartRef", {}) or
                   hr.get("spec", {}).get("chart", {})
                      .get("spec", {}).get("sourceRef", {}))
            sk, sn = ref.get("kind", "").lower(), ref.get("name", "")
            sns    = ref.get("namespace", namespace)
            if sk in KIND_MAP and sn:
                sg, sv, sp = KIND_MAP[sk]
                src_result = _annotate(sg, sv, sp, sn, sns)
        except Exception as e:
            src_result = {"error": str(e)}

    result = _annotate(g, v, p, name, namespace)
    result.update({
        "withSource":   with_source,
        "sourceResult": src_result,
        "message": (
            f"HelmRelease/{name} triggered"
            + (" with source" if with_source else "")
            + ". Flux will re-run Helm upgrade. ~60s to complete."),
    })
    return result


# ══════════════════════════════════════════════════════════════════
# SUSPEND / RESUME
# ══════════════════════════════════════════════════════════════════

def _set_suspend(kind: str, name: str, namespace: str,
                 value: bool) -> dict:
    api, _, _ = get_client()
    k = kind.lower()
    if k not in KIND_MAP:
        return {"error": f"Unknown kind '{kind}'",
                "allowed": list(KIND_MAP.keys())}
    g, v, p = KIND_MAP[k]
    try:
        api.patch_namespaced_custom_object(
            g, v, namespace, p, name,
            body={"spec": {"suspend": value}})
        action = "suspended" if value else "resumed"
        return {
            "status": action, "kind": kind,
            "name": name, "namespace": namespace,
            "message": f"{kind}/{name} is now {action}.",
        }
    except ApiException as e:
        return {"error": str(e)}


def suspend_flux_reconciliation(kind: str, name: str,
                                namespace: str) -> dict:
    """
    SUSPEND — Stop Flux reconciling a resource (spec.suspend=true).
    Use to pause a broken resource while fixing the root cause.
    Supports all Flux CRD kinds.
    """
    return _set_suspend(kind, name, namespace, True)


def resume_flux_reconciliation(kind: str, name: str,
                               namespace: str) -> dict:
    """
    RESUME — Re-enable reconciliation (spec.suspend=false).
    Use after fixing the root cause. Call reconcile after
    this to trigger immediately without waiting for the interval.
    Supports all Flux CRD kinds.
    """
    return _set_suspend(kind, name, namespace, False)
