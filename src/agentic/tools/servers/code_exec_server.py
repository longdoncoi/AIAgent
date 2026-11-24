"""FastMCP / MCPServer for Sandboxed Code Execution (Port 9001)."""

import argparse
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path

import httpx

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)
mcp = FastMCP(
    "code_exec_server", instructions="Executes Python and shell code in a controlled environment."
)

# UX-only guardrails. A substring denylist is NOT a security boundary for a
# general-purpose execution tool; real isolation is the gVisor/Docker sandbox.
PROHIBITED_COMMAND_PATTERNS = [
    "rm -rf /",
    "mkfs",
    ":(){ :|:& };:",
    "dd if=/dev/zero",
    "shutdown",
    "reboot",
    "init 0",
    "/etc/shadow",
    "/etc/passwd",
    "pty.spawn",
    "socket.socket",
    "nc -e",
    "bash -i",
]


def _bounded_timeout(timeout: int) -> int:
    return max(1, min(timeout, get_settings().max_execution_timeout_seconds, 60))


def _build_isolated_env() -> dict[str, str]:
    """Scrubs sensitive credentials and API keys from child environment."""
    safe_keys = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TMP",
        "TEMP",
        "PYTHONIOENCODING",
        "LC_ALL",
        "LANG",
    }
    return {k: v for k, v in os.environ.items() if k.upper() in safe_keys}


def _k8s_api_config() -> tuple[str, str, str] | None:
    """Return in-cluster API endpoint, token and CA certificate paths."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    token_path = os.environ.get(
        "KUBERNETES_SERVICE_ACCOUNT_TOKEN_FILE",
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
    )
    ca_path = os.environ.get(
        "KUBERNETES_SERVICE_ACCOUNT_CA_FILE",
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    )
    if not host or not Path(token_path).is_file() or not Path(ca_path).is_file():
        return None
    return f"https://{host}:{port}", token_path, ca_path


def _is_k8s_available() -> bool:
    """Check whether the in-cluster Kubernetes API credentials are available."""
    settings = get_settings()
    if not (settings.enable_k8s_sandbox or "KUBERNETES_SERVICE_HOST" in os.environ):
        return False
    return _k8s_api_config() is not None


async def _run_k8s_pod(
    pod: Mapping[str, object], namespace: str, pod_name: str, timeout: int
) -> tuple[str, int]:
    """Create, monitor and remove an ephemeral sandbox Pod via the Kubernetes API."""
    config = _k8s_api_config()
    if config is None:
        raise RuntimeError("Kubernetes in-cluster API credentials are unavailable")

    api_url, token_path, ca_path = config
    token = Path(token_path).read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"}
    pod_path = f"/api/v1/namespaces/{namespace}/pods"
    instance_path = f"{pod_path}/{pod_name}"
    created = False

    async with httpx.AsyncClient(
        base_url=api_url,
        headers=headers,
        verify=ca_path,
        timeout=httpx.Timeout(min(timeout, 10)),
    ) as client:
        try:
            response = await client.post(pod_path, json=pod)
            response.raise_for_status()
            created = True

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                if loop.time() >= deadline:
                    raise asyncio.TimeoutError
                response = await client.get(instance_path)
                response.raise_for_status()
                status = response.json().get("status", {})
                phase = status.get("phase")
                if phase in {"Succeeded", "Failed"}:
                    container_statuses = status.get("containerStatuses") or []
                    terminated = (
                        container_statuses[0].get("state", {}).get("terminated", {})
                        if container_statuses
                        else {}
                    )
                    exit_code = int(terminated.get("exitCode", 1 if phase == "Failed" else 0))
                    break
                await asyncio.sleep(0.25)

            response = await client.get(f"{instance_path}/log")
            response.raise_for_status()
            return response.text, exit_code
        finally:
            if created:
                try:
                    await client.delete(
                        instance_path,
                        params={"gracePeriodSeconds": "0", "propagationPolicy": "Background"},
                    )
                except Exception:
                    logger.warning("Failed to clean up Kubernetes sandbox pod %s", pod_name)


async def _execute_in_k8s_sandbox(code_or_cmd: str, is_python: bool, timeout: int = 30) -> str:
    """Executes execution payload inside an isolated ephemeral Kubernetes Pod with gVisor sandbox."""
    settings = get_settings()
    pod_name = f"sandbox-{uuid.uuid4().hex[:8]}"
    timeout = _bounded_timeout(timeout)
    image = (
        "python:3.12-slim@sha256:a0e2dd78cc679ddfffea960fe45ecfcff9ac2f68d67917a0edfcde5c2100151b"
        if is_python
        else "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
    )
    command_flag = ["python", "-c", code_or_cmd] if is_python else ["sh", "-c", code_or_cmd]

    overrides = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": settings.k8s_sandbox_namespace,
            "labels": {"app": "agentic-sandbox", "sandbox": "untrusted"},
        },
        "spec": {
            "runtimeClassName": settings.k8s_sandbox_runtime_class,
            "automountServiceAccountToken": False,
            "activeDeadlineSeconds": timeout + 5,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "runner",
                    "image": image,
                    "command": command_flag,
                    "resources": {
                        "limits": {"cpu": "500m", "memory": "256Mi"},
                        "requests": {"cpu": "100m", "memory": "64Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "runAsNonRoot": True,
                        "runAsUser": 65534,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                }
            ],
            "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}}],
        },
    }

    try:
        out_str, exit_code = await asyncio.wait_for(
            _run_k8s_pod(
                overrides,
                settings.k8s_sandbox_namespace,
                pod_name,
                timeout,
            ),
            timeout=timeout + 2,
        )
        output = [f"[KUBERNETES GVISOR SANDBOX ACTIVE: pod={pod_name}]"]
        if out_str:
            output.append(f"STDOUT:\n{out_str}")
        output.append(f"Exit Code: {exit_code}")
        return "\n".join(output)
    except asyncio.TimeoutError:
        return f"Kubernetes sandbox execution timed out after {timeout} seconds."
    except Exception as e:
        logger.warning(f"Kubernetes sandbox execution error ({e}). Attempting Docker fallback.")
        if _is_docker_available():
            return await _execute_in_docker(code_or_cmd, is_python=is_python, timeout=timeout)
        raise


def _is_docker_available() -> bool:
    """Checks whether Docker daemon is active and usable for containerized sandbox."""
    settings = get_settings()
    if not settings.enable_docker_sandbox:
        return False
    if not shutil.which("docker"):
        return False
    try:
        res = shutil.which("docker")
        return bool(res)
    except Exception:
        return False


async def _execute_in_docker(code_or_cmd: str, is_python: bool, timeout: int = 30) -> str:
    """Executes execution payload inside an isolated, un-networked ephemeral Docker container."""
    timeout = _bounded_timeout(timeout)
    container_name = f"agentic-sandbox-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        script_file = tmp_path / ("script.py" if is_python else "run.sh")
        with open(script_file, "w", encoding="utf-8") as f:
            f.write(code_or_cmd)

        docker_args = [
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            "-v",
            f"{script_file.resolve()}:/app/run_target:ro",
            (
                "python:3.12-slim@sha256:a0e2dd78cc679ddfffea960fe45ecfcff9ac2f68d67917a0edfcde5c2100151b"
                if is_python
                else "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
            ),
        ]
        if is_python:
            docker_args.extend(["python", "/app/run_target"])
        else:
            docker_args.extend(["sh", "/app/run_target"])

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out_str = stdout.decode("utf-8", errors="replace")
            err_str = stderr.decode("utf-8", errors="replace")
            output = ["[DOCKER SANDBOX ISOLATION ACTIVE]"]
            if out_str:
                output.append(f"STDOUT:\n{out_str}")
            if err_str:
                output.append(f"STDERR:\n{err_str}")
            output.append(f"Exit Code: {proc.returncode}")
            return "\n".join(output)
        except asyncio.TimeoutError:
            try:
                if proc is not None:
                    proc.kill()
                    await proc.wait()
                cleanup = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await cleanup.wait()
            except Exception:
                pass
            return f"Execution timed out in container after {timeout} seconds."
        except Exception as e:
            return f"Docker execution error: {str(e)}"


async def execute_python_code(code: str, timeout: int = 30) -> str:
    """Executes Python code in an isolated environment with scrubbed credentials."""
    timeout = _bounded_timeout(timeout)
    # Check for obvious destructive commands
    for pattern in PROHIBITED_COMMAND_PATTERNS:
        if pattern in code:
            return f"Security Exception: Execution blocked due to prohibited pattern: '{pattern}'."

    # 1. Prefer Kubernetes gVisor Pod Sandbox if in Kubernetes cluster
    if _is_k8s_available():
        try:
            return await _execute_in_k8s_sandbox(code, is_python=True, timeout=timeout)
        except Exception as e:
            logger.warning(f"K8s sandbox execution failed: {e}")

    # 2. Prefer Docker container isolation if local Docker daemon is available
    if _is_docker_available():
        return await _execute_in_docker(code, is_python=True, timeout=timeout)

    settings = get_settings()
    if settings.environment == "production" or not settings.allow_host_code_execution:
        return (
            "Security Exception: Host subprocess execution is strictly disabled in production. "
            "An isolated container sandbox (Kubernetes gVisor / Docker) is required."
        )

    safe_env = _build_isolated_env()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmp_dir,
                env=safe_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out_str = stdout.decode("utf-8", errors="replace")
            err_str = stderr.decode("utf-8", errors="replace")

            output = ["[SUBPROCESS ISOLATION]"]
            if out_str:
                output.append(f"STDOUT:\n{out_str}")
            if err_str:
                output.append(f"STDERR:\n{err_str}")
            output.append(f"Exit Code: {proc.returncode}")
            return "\n".join(output) if output else "Executed with no output (Exit Code: 0)"
        except asyncio.TimeoutError:
            try:
                if proc is not None:
                    proc.kill()
                    res_w = proc.wait()
                    if asyncio.iscoroutine(res_w):
                        await res_w
            except Exception:
                pass
            return f"Execution timed out after {timeout} seconds."
        except Exception as e:
            return f"Execution error: {str(e)}"


async def execute_bash_command(command: str, timeout: int = 30) -> str:
    """Executes shell command in an isolated environment with scrubbed credentials."""
    timeout = _bounded_timeout(timeout)
    for pattern in PROHIBITED_COMMAND_PATTERNS:
        if pattern in command:
            return f"Security Exception: Execution blocked due to prohibited pattern: '{pattern}'."

    # 1. Prefer Kubernetes gVisor Pod Sandbox if in Kubernetes cluster
    if _is_k8s_available():
        try:
            return await _execute_in_k8s_sandbox(command, is_python=False, timeout=timeout)
        except Exception as e:
            logger.warning(f"K8s sandbox execution failed: {e}")

    # 2. Prefer Docker container isolation if local Docker daemon is available
    if _is_docker_available():
        return await _execute_in_docker(command, is_python=False, timeout=timeout)

    settings = get_settings()
    if settings.environment == "production" or not settings.allow_host_code_execution:
        return (
            "Security Exception: Host shell execution is strictly disabled in production. "
            "An isolated container sandbox (Kubernetes gVisor / Docker) is required."
        )

    safe_env = _build_isolated_env()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmp_dir,
                env=safe_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out_str = stdout.decode("utf-8", errors="replace")
            err_str = stderr.decode("utf-8", errors="replace")

            output = []
            if out_str:
                output.append(f"STDOUT:\n{out_str}")
            if err_str:
                output.append(f"STDERR:\n{err_str}")
            output.append(f"Exit Code: {proc.returncode}")
            return "\n".join(output) if output else "Command executed with no output."
        except asyncio.TimeoutError:
            try:
                if proc is not None:
                    proc.kill()
                    res_w = proc.wait()
                    if asyncio.iscoroutine(res_w):
                        await res_w
            except Exception:
                pass
            return f"Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Command execution error: {str(e)}"


@mcp.tool()
async def code_exec_python(code: str, timeout: int = 30) -> str:
    """Execute Python code in an isolated subprocess.

    Args:
        code: The Python script string to execute.
        timeout: Maximum execution time in seconds.
    """
    return await execute_python_code(code, timeout=timeout)


@mcp.tool()
async def code_exec_bash(command: str, timeout: int = 30) -> str:
    """Execute a bash or shell command.

    Args:
        command: The terminal command string.
        timeout: Maximum execution time in seconds.
    """
    return await execute_bash_command(command, timeout=timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Code Execution MCP Server")
    parser.add_argument("--port", type=int, default=9001, help="Port to listen on (SSE mode)")
    parser.add_argument(
        "--transport", type=str, default="stdio", choices=["stdio", "sse"], help="MCP transport"
    )
    args = parser.parse_args()

    if args.transport == "sse":
        from typing import Any, cast

        import uvicorn

        app = getattr(mcp, "sse_app", None) or getattr(mcp, "streamable_http_app", None)
        if callable(app):
            from src.agentic.tools.servers.security import secure_mcp_app

            uvicorn.run(secure_mcp_app(app()), host="0.0.0.0", port=args.port)
        else:
            from src.agentic.tools.servers.security import secure_mcp_app

            uvicorn.run(secure_mcp_app(cast(Any, mcp)), host="0.0.0.0", port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
