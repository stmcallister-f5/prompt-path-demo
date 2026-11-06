"""How a toggle becomes a change in the cluster.

Two mechanisms, both visible with plain kubectl while you demo:

slot      Each position in a chain is fronted by a "slot" Service. Turning a
          layer off patches that Service's selector to the next component
          that is on, so traffic skips the layer. `kubectl get svc -o wide -w`
          shows it happen.
manifest  Turning a layer on applies a Kubernetes object (a WAF policy, a
          route filter, anything); turning it off deletes it. Use this for
          layers that live inside another component, like a WAF policy
          attached to NGINX Gateway Fabric.
"""
from __future__ import annotations

import asyncio
import time
from typing import Protocol

import httpx
import yaml


class Driver(Protocol):
    name: str
    async def get_selector(self, service: str) -> str | None: ...
    async def set_selector(self, service: str, component: str) -> None: ...
    async def wait_ready(self, service: str, component: str, timeout: float) -> float: ...
    async def manifest_exists(self, manifest: dict) -> bool: ...
    async def manifest_apply(self, manifest: dict) -> None: ...
    async def manifest_delete(self, manifest: dict) -> None: ...
    def describe(self) -> str: ...


class LocalDriver:
    """Talks to demo.devrouter instead of the Kubernetes API (laptop mode)."""
    name = "local"

    def __init__(self, router_url: str, key: str):
        self.url, self.key = router_url.rstrip("/"), key
        self.objects: set[str] = set()

    async def get_selector(self, service):
        async with httpx.AsyncClient() as c:
            return (await c.get(f"{self.url}/_slots")).json().get(service)

    async def set_selector(self, service, component):
        async with httpx.AsyncClient() as c:
            (await c.put(f"{self.url}/_slots/{service}", json={"component": component})).raise_for_status()

    async def wait_ready(self, service, component, timeout):
        return 0.0

    @staticmethod
    def _k(m):
        md = m.get("metadata", {})
        return f"{m.get('kind')}/{md.get('namespace', '')}/{md.get('name')}"

    async def manifest_exists(self, manifest):
        return self._k(manifest) in self.objects

    async def manifest_apply(self, manifest):
        self.objects.add(self._k(manifest))

    async def manifest_delete(self, manifest):
        self.objects.discard(self._k(manifest))

    def describe(self):
        return f"local devrouter at {self.url}"


class KubeDriver:
    name = "kubernetes"

    def __init__(self, namespace: str, key: str, settle: float = 1.0):
        from kubernetes import client, config
        from kubernetes.dynamic import DynamicClient
        try:
            config.load_incluster_config()
            self.where = "in-cluster"
        except config.ConfigException:
            config.load_kube_config()
            self.where = "kubeconfig"
        self.api = client.ApiClient()
        self.core = client.CoreV1Api(self.api)
        self.discovery = client.DiscoveryV1Api(self.api)
        self.dyn = DynamicClient(self.api)
        self.ns, self.key, self.settle = namespace, key, settle

    async def _run(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    async def get_selector(self, service):
        svc = await self._run(self.core.read_namespaced_service, service, self.ns)
        return (svc.spec.selector or {}).get(self.key)

    async def set_selector(self, service, component):
        svc = await self._run(self.core.read_namespaced_service, service, self.ns)
        selector = dict(svc.spec.selector or {})
        selector[self.key] = component
        patch = [{"op": "replace", "path": "/spec/selector", "value": selector}]
        await self._run(self.core.patch_namespaced_service, service, self.ns, patch)

    async def _endpoint_components(self, service) -> set[str] | None:
        slices = await self._run(self.discovery.list_namespaced_endpoint_slice, self.ns,
                                 label_selector=f"kubernetes.io/service-name={service}")
        names = []
        for s in slices.items:
            for ep in s.endpoints or []:
                ready = ep.conditions is None or ep.conditions.ready is not False
                if ready and ep.target_ref and ep.target_ref.kind == "Pod":
                    names.append(ep.target_ref.name)
        if not names:
            return None
        comps = set()
        for n in names:
            pod = await self._run(self.core.read_namespaced_pod, n, self.ns)
            comps.add((pod.metadata.labels or {}).get(self.key))
        return comps

    async def wait_ready(self, service, component, timeout):
        """Wait until the slot's EndpointSlices hold only ready pods of `component`."""
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            comps = await self._endpoint_components(service)
            if comps == {component}:
                await asyncio.sleep(self.settle)        # let kube-proxy / the gateway catch up
                return (time.perf_counter() - t0) * 1000
            await asyncio.sleep(0.2)
        raise TimeoutError(f"service/{service} did not converge on {component} in {timeout}s")

    def _res(self, m):
        return self.dyn.resources.get(api_version=m["apiVersion"], kind=m["kind"])

    async def manifest_exists(self, manifest):
        from kubernetes.dynamic.exceptions import NotFoundError
        md = manifest["metadata"]
        try:
            await self._run(self._res(manifest).get, name=md["name"], namespace=md.get("namespace", self.ns))
            return True
        except NotFoundError:
            return False

    async def manifest_apply(self, manifest):
        md = manifest["metadata"]
        await self._run(self._res(manifest).server_side_apply, body=manifest, name=md["name"],
                        namespace=md.get("namespace", self.ns), field_manager="toggle-board", force_conflicts=True)

    async def manifest_delete(self, manifest):
        from kubernetes.dynamic.exceptions import NotFoundError
        md = manifest["metadata"]
        try:
            await self._run(self._res(manifest).delete, name=md["name"], namespace=md.get("namespace", self.ns))
        except NotFoundError:
            pass

    def describe(self):
        return f"kubernetes ({self.where}) · namespace {self.ns}"


def load_manifest(layer: dict) -> dict:
    m = layer.get("manifest")
    return yaml.safe_load(m) if isinstance(m, str) else m
