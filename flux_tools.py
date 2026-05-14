"""
flux_tools.py
─────────────
READ-ONLY Flux CD troubleshooting tools.
No write, patch, delete, or reconcile operations.

Modelled on Flux Operator MCP reporting tools:
  https://fluxoperator.dev/docs/mcp/tools/#reporting-tools

Tools:
  get_flux_instance           — controller health
  get_kubernetes_resources    — any Flux CRD or core resource
  get_kubernetes_logs         — pod logs
  get_kubernetes_metrics      — CPU/memory
  get_kubernetes_api_versions — installed CRDs

Namespaces: flux-system, nwdaf-cd
"""

import datetime
from typing import Optional
from kubernetes.client.rest import ApiException
from kube_client import get_client, test_connection  # noqa: F401

NAMESPACES = ["flux-system", "nwdaf-cd"]

FLUX_CONTROLLERS = [
    "source-controller",
    "kustomize-controller",
    "helm-controller",
    "notification-controller",
    "image-reflector-controller",
    "image-automation-controller",
]

KIND_MAP = {
    "kustomization":         ("kustomize.toolkit.fluxcd.io",    "v1",      "kustomizations"),
    "helmrelease":           ("helm.toolkit.fluxcd.io",          "v2",      "helmreleases"),
    "gitrepository":         ("source.toolkit.fluxcd.io",        "v1",      "gitrepositories"),
    "helmrepository":        ("source.toolkit.fluxcd.io",        "v1",      "helmrepositories"),
    "helmchart":             ("source.toolkit.fluxcd.io",        "v1",      "helmcharts"),
    "ocirepository":         ("source.toolkit.fluxcd.io",        "v1",      "ocirepositories"),
    "bucket":                ("source.toolkit.fluxcd.io",        "v1",      "buckets"),
    "alert":                 ("notification.toolkit.fluxcd.io",  "v1beta3", "alerts"),
    "provider":              ("notification.toolkit.fluxcd.io",  "v1beta3", "providers"),
    "receiver":              ("notification.toolkit.fluxcd.io",  "v1",      "receivers"),
    "imagerepository":       ("image.toolkit.fluxcd.io",         "v1beta2", "imagerepositories"),
    "imagepolicy":           ("image.toolkit.fluxcd.io",         "v1beta2", "imagepolicies"),
    "imageupdateautomation": ("image.toolkit.fluxcd.io",         "v1beta1", "imageupdateautomations"),
}


# ── helpers ───────────────────────────────────────────────────────

def _ready(conditions: list) -> dict:
    for c in conditions:
        if c.get("type") == "Ready":
            return {
                "status":             c.get("status",             "Unknown"),
                "reason":             c.get("reason",             ""),
                "message":            c.get("message",            ""),
                "lastTransitionTime": c.get("lastTransitionTime", ""),
            }
    return {"status": "Unknown", "reason": "", "message": "",
            "lastTransitionTime": ""}


def _summarise(item: dict) -> dict:
    meta   = item.get("metadata", {})
    spec   = item.get("spec",     {})
    status = item.get("status",   {})
    r      = _ready(status.get("conditions", []))
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
        "spec":   spec,    # LLM inspects sourceRef, valuesFrom, etc.
        "status": status,  # LLM inspects inventory, conditions, etc.
    }


# ══════════════════════════════════════════════════════════════════
# REPORTING — all read-only
# ══════════════════════════════════════════════════════════════════

def get_flux_instance() -> dict:
    """
    READ-ONLY — Flux controller health.
    Returns status, image version, replicas, restart count
    for every Flux controller deployment.
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
            "total":                len(results),
            "healthy":              len(results) - len(unhealthy),
            "unhealthy":            len(unhealthy),
            "allHealthy":           len(unhealthy) == 0,
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
    READ-ONLY — Retrieve any Flux CRD or core Kubernetes resource.
    Returns full spec, status, conditions and inventory.
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
                            "tip":   f"Namespaces searched: {NAMESPACES}"}
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

    # ── Core resources ─────────────────────────────────────────────
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
                        "matchLabels": d.spec.selector.match_labels or {},
                    } for d in deps]}
        except ApiException as e:
            return {"error": str(e)}

    if k == "event":
        try:
            field = (f"involvedObject.name={name}"
                     if name else "type!=Normal")
            evs   = core.list_namespaced_event(
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
        # Return names and types only — never values
        try:
            items = core.list_namespaced_secret(ns, limit=limit).items
            return {"kind": "Secret", "namespace": ns,
                    "note": "Values not returned for security",
                    "secrets": [{"name": s.metadata.name, "type": s.type}
                                for s in items]}
        except ApiException as e:
            return {"error": str(e)}

    return {
        "error":     f"kind '{kind}' not supported",
        "supported": list(KIND_MAP.keys()) + [
            "pod", "deployment", "event", "configmap", "secret"
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
    READ-ONLY — Get logs from a pod container.
    previous=True fetches logs from crashed/restarted instance.
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
        return {"error": str(e)}


def get_kubernetes_metrics(
    pod_namespace: str,
    pod_name: Optional[str]     = None,
    pod_selector: Optional[str] = None,
    limit: int                  = 20,
) -> dict:
    """READ-ONLY — CPU and Memory usage. Needs metrics-server."""
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
            return {"error": "metrics-server not installed"}
        return {"error": str(e)}


def get_kubernetes_api_versions() -> dict:
    """READ-ONLY — List all Flux CRDs with their preferred apiVersion."""
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
