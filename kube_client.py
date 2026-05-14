"""
kube_client.py — Read-only cluster connection.
Loads kubeconfig from KUBECONFIG env var or ~/.kube/config.
Only returns read-only API clients — no write operations.
"""
import os
from kubernetes import client, config
from kubernetes.client.rest import ApiException


def get_client():
    """
    Returns (CustomObjectsApi, CoreV1Api, AppsV1Api).
    These are used READ-ONLY throughout the codebase.
    No patch/create/delete calls are made anywhere.
    """
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
