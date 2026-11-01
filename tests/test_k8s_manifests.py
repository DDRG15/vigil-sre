"""
tests/test_k8s_manifests.py -- the Kubernetes manifests run what compose runs.

deploy/k8s/ runs the monitor and the dashboard in one pod, on one volume, the
way compose runs them on one directory. Each assertion is a property that, if
lost, would fail quietly in a cluster:

  - One replica, recreated, never rolled. Two monitors would send every alert
    twice and write the same SQLite file from two processes; a RollingUpdate
    runs two for as long as the new pod takes to start.
  - The image's uid. The volume is owned through fsGroup by the same uid the
    Dockerfile pins and the README grants on a Linux host.
  - The same loop. The monitor's shell loop is the compose one, character for
    character once compose's `$$` escape is undone; a fix to one that missed
    the other is how two deployments drift apart.
  - A stop long enough to finish an alert delivery, as compose's.
  - No secret in the repository: the pod reads an optional Secret created from
    .env on the cluster.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S  = ROOT / "deploy" / "k8s"
PINNED = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def _docs(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((K8S / name).read_text(encoding="utf-8")) if d]


def _all_docs() -> list[dict]:
    return [d for f in sorted(K8S.glob("*.yaml")) for d in _docs(f.name)]


def _one(kind: str, name: str) -> dict:
    found = [d for d in _all_docs() if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind}/{name}: {len(found)} found"
    return found[0]


def _deploy() -> dict:
    return _one("Deployment", "vigil-sre")


def _pod() -> dict:
    return _deploy()["spec"]["template"]["spec"]


def _container(name: str) -> dict:
    return next(c for c in _pod()["containers"] if c["name"] == name)


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_every_manifest_parses_and_names_a_namespace() -> None:
    docs = _all_docs()
    assert docs
    for d in docs:
        if d["kind"] != "Namespace":
            assert d["metadata"].get("namespace"), f"{d['kind']}/{d['metadata']['name']}"


def test_one_replica_recreated_never_rolled() -> None:
    spec = _deploy()["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate"


def test_the_pod_runs_as_the_uid_the_dockerfile_pins() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    uid = int(re.search(r"--uid[ =](\d+)", dockerfile).group(1))
    ctx = _pod()["securityContext"]
    assert ctx["runAsUser"] == ctx["runAsGroup"] == ctx["fsGroup"] == uid
    assert ctx["runAsNonRoot"] is True


def test_every_container_is_locked_down() -> None:
    for c in _pod()["containers"]:
        ctx = c["securityContext"]
        assert ctx["allowPrivilegeEscalation"] is False, c["name"]
        assert ctx["readOnlyRootFilesystem"] is True, c["name"]
        assert ctx["capabilities"]["drop"] == ["ALL"], c["name"]


def test_both_containers_share_one_volume_and_the_dashboard_reads_history_only() -> None:
    claim = _pod()["volumes"]
    data = next(v for v in claim if v["name"] == "data")
    assert data["persistentVolumeClaim"]["claimName"] == "vigil-sre-data"
    _one("PersistentVolumeClaim", "vigil-sre-data")
    monitor = {m["mountPath"]: m for m in _container("monitor")["volumeMounts"]}
    dash    = {m["mountPath"]: m for m in _container("dashboard")["volumeMounts"]}
    assert monitor["/app/data"]["name"] == dash["/app/data"]["name"] == "data"
    assert dash["/app/data/history"].get("readOnly") is True
    assert dash["/app/data/history"]["subPath"] == "history"


def test_the_paths_are_composes() -> None:
    for service, container in (("health-checker", "monitor"), ("dashboard", "dashboard")):
        want = dict(e.split("=", 1) for e in _compose()["services"][service]["environment"])
        have = {e["name"]: e["value"] for e in _container(container)["env"]}
        for key in ("STATE_FILE_PATH", "HISTORY_DB_PATH"):
            assert have[key] == want[key], (container, key)


def test_the_monitor_loop_is_the_compose_loop() -> None:
    compose_loop = _compose()["services"]["health-checker"]["command"][2].replace("$$", "$")
    k8s_loop     = _container("monitor")["command"][2]
    assert k8s_loop.strip() == compose_loop.strip()


def test_the_monitor_logs_to_stdout_only_on_a_read_only_root() -> None:
    """Found by running it: with readOnlyRootFilesystem the default
    health_checker.log could not be opened, and every round crashed at import.
    The pod's log is what the kubelet collects from stdout."""
    env = {e["name"]: e["value"] for e in _container("monitor")["env"]}
    assert env["LOG_FILE"] == ""


def test_a_stop_may_last_as_long_as_composes() -> None:
    grace = int(_compose()["services"]["health-checker"]["stop_grace_period"].rstrip("s"))
    assert _pod()["terminationGracePeriodSeconds"] >= grace


def test_no_secret_lives_in_the_repository() -> None:
    assert not [d for d in _all_docs() if d["kind"] == "Secret"]
    for c in _pod()["containers"]:
        refs = [e["secretRef"] for e in c.get("envFrom", []) if "secretRef" in e]
        assert refs and all(r["name"] == "vigil-sre-env" and r["optional"] is True for r in refs), c["name"]


def test_third_party_images_are_pinned_and_ours_is_not_latest() -> None:
    for d in _all_docs():
        if d["kind"] != "Deployment":
            continue
        for c in d["spec"]["template"]["spec"]["containers"]:
            image = c["image"]
            if image.startswith("vigil-sre:"):
                assert not image.endswith(":latest")
            else:
                assert PINNED.match(image), image


def test_the_dashboard_service_selects_the_pod() -> None:
    svc = _one("Service", "vigil-sre-dashboard")
    labels = _deploy()["spec"]["template"]["metadata"]["labels"]
    assert all(labels.get(k) == v for k, v in svc["spec"]["selector"].items())
    assert svc["spec"]["ports"][0]["targetPort"] == 8787


def test_the_seed_targets_include_an_in_cluster_service() -> None:
    seed = yaml.safe_load(_one("ConfigMap", "vigil-sre-targets")["data"]["targets.yaml"])
    urls = [t["url"] for t in seed["targets"]]
    assert any(".svc.cluster.local" in u for u in urls), urls
    echo = _one("Service", "echo")
    assert echo["metadata"]["namespace"] == "vigil-demo"
