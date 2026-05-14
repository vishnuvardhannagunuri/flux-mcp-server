"""
kube_client.py — Cluster connection helper.
Reads kubeconfig from KUBECONFIG env var or ~/.kube/config.
"""
import os
from kubernetes import client, config
from kubernetes.client.rest import ApiException


def get_client():
    """Returns (CustomObjectsApi, CoreV1Api, AppsV1Api)."""
    path = os.getenv("KUBECONFIG", os.path.expanduser("~/.kube/config"))
    try:
        config.load_kube_config(config_file=path)
    except Exception as e:
        raise RuntimeError(f"Cannot load kubeconfig from {path}: {e}")
    return (
        client.CustomObjectsApi(),
        client.CoreV1Api(),
        client.AppsV1Api(),
    )


def test_connection() -> dict:
    try:
        _, core, _ = get_client()
        ns = core.list_namespace(limit=5)
        return {
            "connected":  True,
            "namespaces": [n.metadata.name for n in ns.items],
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}
