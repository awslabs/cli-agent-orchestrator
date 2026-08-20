"""Narrow worker-Job broker for the CAO elastic Kubernetes topology."""

from __future__ import annotations

import hmac
import os
import secrets
import time
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, status
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

app = FastAPI(title="CAO Elastic Worker Broker")

NAMESPACE = os.environ.get("CAO_ELASTIC_NAMESPACE", "cao-cluster")
WORKER_IMAGE = os.environ["CAO_ELASTIC_WORKER_IMAGE"]
WORKSPACE_PVC = os.environ.get("CAO_ELASTIC_WORKSPACE_PVC", "cao-elastic-workspace")
MEMORY_API_URL = os.environ.get("CAO_MEMORY_API_URL", "http://cao-supervisor:9889")
BROKER_PUBLIC_URL = os.environ.get("CAO_ELASTIC_BROKER_URL", "http://cao-worker-broker:9890")
BROKER_TOKEN = os.environ["CAO_ELASTIC_BROKER_TOKEN"]
WORKSPACE_ROOT = os.environ.get(
    "CAO_ELASTIC_WORKSPACE_ROOT", "/home/cao/workspace/jobs"
)
PROJECT_ID = os.environ.get("CAO_ELASTIC_PROJECT_ID", "cao-cluster")
WORKER_TIMEOUT = int(os.environ.get("CAO_ELASTIC_WORKER_TIMEOUT", "3600"))
READY_TIMEOUT = int(os.environ.get("CAO_ELASTIC_READY_TIMEOUT", "300"))

config.load_incluster_config()
batch_api = client.BatchV1Api()
core_api = client.CoreV1Api()


class WorkerRequest(BaseModel):
    agent_profile: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    provider: str = Field(default="kiro_cli", pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class WorkerLease(BaseModel):
    worker_id: str
    target_host: str
    working_directory: str
    release_token: str


def _require_broker_token(value: Optional[str]) -> None:
    if not value or not hmac.compare_digest(value, BROKER_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


def _job_name(worker_id: str) -> str:
    return f"cao-worker-{worker_id}"


def _labels(worker_id: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "cao-elastic-worker",
        "app.kubernetes.io/part-of": "cao-elastic-fleet",
        "cao.aws/worker-id": worker_id,
    }


def _worker_job(
    worker_id: str,
    release_token: str,
    request: WorkerRequest,
) -> client.V1Job:
    name = _job_name(worker_id)
    labels = _labels(worker_id)
    working_directory = f"{WORKSPACE_ROOT}/{worker_id}"
    env = [
        client.V1EnvVar(name="CAO_BIND_HOST", value="0.0.0.0"),
        client.V1EnvVar(name="CAO_API_PORT", value="9889"),
        client.V1EnvVar(
            name="CAO_ALLOWED_HOSTS",
            value=f"{name},{name}.{NAMESPACE}.svc.cluster.local,localhost",
        ),
        client.V1EnvVar(name="CAO_MAX_TERMINALS", value="1"),
        client.V1EnvVar(name="CAO_HOME_DIR", value="/home/cao/.cao/state"),
        client.V1EnvVar(
            name="CAO_INSTALL_PROFILES",
            value=f"{request.agent_profile}:{request.provider}",
        ),
        client.V1EnvVar(name="CAO_MEMORY_API_URL", value=MEMORY_API_URL),
        client.V1EnvVar(name="CAO_PROJECT_ID", value=PROJECT_ID),
        client.V1EnvVar(name="CAO_ELASTIC_WORKER_ID", value=worker_id),
        client.V1EnvVar(name="CAO_ELASTIC_BROKER_URL", value=BROKER_PUBLIC_URL),
        client.V1EnvVar(name="CAO_ELASTIC_RELEASE_TOKEN", value=release_token),
        client.V1EnvVar(name="CAO_ELASTIC_WORKING_DIRECTORY", value=working_directory),
        client.V1EnvVar(
            name="KIRO_API_KEY",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name="kiro-api-key",
                    key="KIRO_API_KEY",
                )
            ),
        ),
    ]
    mounts = [
        client.V1VolumeMount(name="state", mount_path="/home/cao/.cao"),
        client.V1VolumeMount(
            name="workspace",
            mount_path="/home/cao/workspace",
        ),
    ]
    init = client.V1Container(
        name="prepare-workspace",
        image="public.ecr.aws/docker/library/busybox:1.36",
        command=["sh", "-c", f"mkdir -p {working_directory}"],
        volume_mounts=[mounts[1]],
        security_context=client.V1SecurityContext(
            run_as_user=1000,
            run_as_group=1000,
        ),
    )
    container = client.V1Container(
        name="cao-node",
        image=WORKER_IMAGE,
        env=env,
        ports=[client.V1ContainerPort(name="http", container_port=9889)],
        volume_mounts=mounts,
        resources=client.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "1Gi"},
            limits={"cpu": "1", "memory": "3Gi"},
        ),
        readiness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(
                path="/health",
                port=9889,
                http_headers=[client.V1HTTPHeader(name="Host", value="localhost")],
            ),
            initial_delay_seconds=10,
            period_seconds=5,
        ),
    )
    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        automount_service_account_token=False,
        service_account_name="cao-elastic-worker",
        security_context=client.V1PodSecurityContext(
            fs_group=1000,
            fs_group_change_policy="OnRootMismatch",
        ),
        init_containers=[init],
        containers=[container],
        volumes=[
            client.V1Volume(
                name="state",
                empty_dir=client.V1EmptyDirVolumeSource(),
            ),
            client.V1Volume(
                name="workspace",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=WORKSPACE_PVC
                ),
            ),
        ],
        termination_grace_period_seconds=30,
    )
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=pod_spec,
    )
    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name,
            labels=labels,
            annotations={"cao.aws/release-token": release_token},
        ),
        spec=client.V1JobSpec(
            template=template,
            backoff_limit=0,
            active_deadline_seconds=WORKER_TIMEOUT,
            ttl_seconds_after_finished=300,
        ),
    )


def _worker_service(worker_id: str) -> client.V1Service:
    name = _job_name(worker_id)
    return client.V1Service(
        metadata=client.V1ObjectMeta(name=name, labels=_labels(worker_id)),
        spec=client.V1ServiceSpec(
            selector={"cao.aws/worker-id": worker_id},
            ports=[client.V1ServicePort(name="http", port=9889, target_port=9889)],
        ),
    )


def _wait_ready(worker_id: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    selector = f"cao.aws/worker-id={worker_id}"
    while time.monotonic() < deadline:
        pods = core_api.list_namespaced_pod(NAMESPACE, label_selector=selector).items
        for pod in pods:
            conditions = pod.status.conditions or []
            if any(c.type == "Ready" and c.status == "True" for c in conditions):
                return
            if pod.status.phase in {"Failed", "Succeeded"}:
                raise RuntimeError(f"worker pod ended before readiness: {pod.status.phase}")
        time.sleep(2)
    raise TimeoutError(f"worker {worker_id} did not become ready in {READY_TIMEOUT}s")


def _release(worker_id: str) -> None:
    name = _job_name(worker_id)
    try:
        batch_api.delete_namespaced_job(
            name,
            NAMESPACE,
            propagation_policy="Foreground",
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
    try:
        core_api.delete_namespaced_service(name, NAMESPACE)
    except ApiException as exc:
        if exc.status != 404:
            raise


def _require_release_token(worker_id: str, value: Optional[str]) -> None:
    try:
        job = batch_api.read_namespaced_job(_job_name(worker_id), NAMESPACE)
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail="worker not found") from exc
        raise
    expected = (job.metadata.annotations or {}).get("cao.aws/release-token", "")
    if not value or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="invalid release token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/workers", response_model=WorkerLease)
def create_worker(
    request: WorkerRequest,
    x_cao_broker_token: Optional[str] = Header(default=None),
) -> WorkerLease:
    _require_broker_token(x_cao_broker_token)
    worker_id = secrets.token_hex(4)
    release_token = secrets.token_urlsafe(32)
    name = _job_name(worker_id)
    core_api.create_namespaced_service(NAMESPACE, _worker_service(worker_id))
    try:
        batch_api.create_namespaced_job(
            NAMESPACE,
            _worker_job(worker_id, release_token, request),
        )
        _wait_ready(worker_id)
    except Exception:
        _release(worker_id)
        raise
    return WorkerLease(
        worker_id=worker_id,
        target_host=f"{name}.{NAMESPACE}.svc.cluster.local",
        working_directory=f"{WORKSPACE_ROOT}/{worker_id}",
        release_token=release_token,
    )


@app.delete("/workers/{worker_id}")
def delete_worker(
    worker_id: str,
    x_cao_broker_token: Optional[str] = Header(default=None),
) -> dict[str, bool]:
    _require_broker_token(x_cao_broker_token)
    _release(worker_id)
    return {"released": True}


@app.post("/workers/{worker_id}/complete")
def complete_worker(
    worker_id: str,
    background_tasks: BackgroundTasks,
    x_cao_release_token: Optional[str] = Header(default=None),
) -> dict[str, bool]:
    _require_release_token(worker_id, x_cao_release_token)
    background_tasks.add_task(_release, worker_id)
    return {"release_scheduled": True}
