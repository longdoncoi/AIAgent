"""FastMCP / MCPServer for Sandboxed Code Execution (Port 9001)."""

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)
mcp = FastMCP(
    "code_exec_server", instructions="Executes Python and shell code in a controlled environment."
)

# Prohibited destructive shell patterns & reverse shells
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


def _is_k8s_available() -> bool:
    """Checks whether Kubernetes cluster runner is available for sandboxed execution."""
    settings = get_settings()
    if settings.enable_k8s_sandbox or "KUBERNETES_SERVICE_HOST" in os.environ:
        return bool(shutil.which("kubectl"))
    return False


async def _execute_in_k8s_sandbox(code_or_cmd: str, is_python: bool, timeout: int = 30) -> str:
    """Executes execution payload inside an isolated ephemeral Kubernetes Pod with gVisor sandbox."""
    settings = get_settings()
    pod_name = f"sandbox-{uuid.uuid4().hex[:8]}"
    image = "python:3.11-slim" if is_python else "alpine:latest"
    command_flag = ["python", "-c", code_or_cmd] if is_python else ["sh", "-c", code_or_cmd]

    overrides = {
        "apiVersion": "v1",
        "spec": {
            "runtimeClassName": settings.k8s_sandbox_runtime_class,
            "automountServiceAccountToken": False,
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
                    },
                }
            ],
        },
    }

    kubectl_args = [
        "run",
        pod_name,
        "--image",
        image,
        f"--namespace={settings.k8s_sandbox_namespace}",
        "--restart=Never",
        "--rm",
        "-i",
        f"--overrides={json.dumps(overrides)}",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            *kubectl_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out_str = stdout.decode("utf-8", errors="replace")
        err_str = stderr.decode("utf-8", errors="replace")
        output = [f"[KUBERNETES GVISOR SANDBOX ACTIVE: pod={pod_name}]"]
        if out_str:
            output.append(f"STDOUT:\n{out_str}")
        if err_str:
            output.append(f"STDERR:\n{err_str}")
        output.append(f"Exit Code: {proc.returncode}")
        return "\n".join(output)
    except asyncio.TimeoutError:
        try:
            del_proc = await asyncio.create_subprocess_exec(
                "kubectl",
                "delete",
                "pod",
                pod_name,
                f"--namespace={settings.k8s_sandbox_namespace}",
                "--grace-period=0",
                "--force",
            )
            await del_proc.communicate()
        except Exception:
            pass
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
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        script_file = tmp_path / ("script.py" if is_python else "run.sh")
        with open(script_file, "w", encoding="utf-8") as f:
            f.write(code_or_cmd)

        docker_args = [
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "512m",
            "--cpus",
            "1.0",
            "-v",
            f"{script_file.resolve()}:/app/run_target:ro",
            "python:3.11-slim" if is_python else "alpine:latest",
        ]
        if is_python:
            docker_args.extend(["python", "/app/run_target"])
        else:
            docker_args.extend(["sh", "/app/run_target"])

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
                proc.kill()
            except Exception:
                pass
            return f"Execution timed out in container after {timeout} seconds."
        except Exception as e:
            return f"Docker execution error: {str(e)}"


async def execute_python_code(code: str, timeout: int = 30) -> str:
    """Executes Python code in an isolated environment with scrubbed credentials."""
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
    if settings.environment == "production":
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
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return f"Execution timed out after {timeout} seconds."
        except Exception as e:
            return f"Execution error: {str(e)}"


async def execute_bash_command(command: str, timeout: int = 30) -> str:
    """Executes shell command in an isolated environment with scrubbed credentials."""
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
    if settings.environment == "production":
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
                proc.kill()
                await proc.wait()
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
            uvicorn.run(app(), host="0.0.0.0", port=args.port)
        else:
            uvicorn.run(cast(Any, mcp), host="0.0.0.0", port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
