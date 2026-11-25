"""Locust load testing suite for Agentic Platform.

Simulates concurrent user chats, SSE token streaming, and HITL approvals.
Release gate: locust -f tests/load/locustfile.py --headless -u 100 -r 10 --run-time 5m --host http://localhost:8000
For staging/production runs, provide ``LOCUST_AUTH_TOKEN`` (or ``LOCUST_JWT_SECRET``).
"""

import os
import random
import time
from typing import Any, ClassVar

from locust import HttpUser, between, events, task

from tests.load.quality_gate import load_gate_failures


@events.quitting.add_listener
def _enforce_release_load_gate(environment: Any, **_kwargs: Any) -> None:
    """Make headless Locust fail closed when any declared SLO is missed."""
    failures = load_gate_failures(environment.stats.total)
    if not failures:
        return
    for failure in failures:
        print(f"LOAD GATE FAILED: {failure}")
    environment.process_exit_code = 1


def _resolve_bearer_token() -> str:
    explicit = os.environ.get("LOCUST_AUTH_TOKEN", "").strip()
    if explicit:
        return explicit
    jwt_secret = os.environ.get("LOCUST_JWT_SECRET", "").strip()
    if jwt_secret:
        import jwt

        now = int(time.time())
        return jwt.encode(
            {
                "sub": "locust-load-tester",
                "role": "user",
                "roles": ["user"],
                "department": "engineering",
                "client_id": "load-test",
                "aud": os.environ.get("JWT_AUDIENCE", "agentic-api"),
                "iss": os.environ.get("JWT_ISSUER", "agentic-platform"),
                "iat": now,
                "exp": now + 3600,
            },
            jwt_secret,
            algorithm="HS256",
        )
    return "admin_token"


class AgenticUser(HttpUser):
    wait_time = between(1, 3)

    SAMPLE_QUERIES: ClassVar[list[str]] = [
        "Xin chào, hệ thống hỗ trợ được những tác vụ gì?",
        "Chính sách bảo mật thông tin nội bộ của công ty quy định thế nào?",
        "Hãy viết một script Python tính số Fibonacci với Memoization.",
        "Quy định về thời gian làm việc từ xa và chế độ đãi ngộ ra sao?",
        "Tạo file test_sample.py và chạy thử nghiệm trong sandbox.",
    ]

    def on_start(self) -> None:
        """Sets up default authentication token."""
        self.headers = {
            "Authorization": f"Bearer {_resolve_bearer_token()}",
            "Content-Type": "application/json",
        }

    @task(3)
    def test_standard_chat(self) -> None:
        """Sends normal synchronous chat request."""
        query = random.choice(self.SAMPLE_QUERIES)
        payload = {
            "query": query,
            "metadata": {"test_client": "locust"},
        }
        with self.client.post(
            "/api/v1/chat", json=payload, headers=self.headers, catch_response=True
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if "answer" in data:
                    resp.success()
                else:
                    resp.failure("Missing 'answer' field in response")
            elif resp.status_code == 429:
                resp.failure("Rate limited during the nominal release load profile")
            else:
                resp.failure(f"Status code: {resp.status_code}")

    @task(2)
    def test_sse_streaming(self) -> None:
        """Sends streaming chat request over SSE."""
        payload = {
            "query": "Tóm tắt ngắn gọn các chính sách bảo mật thông tin.",
            "metadata": {"stream": True},
        }
        with self.client.post(
            "/api/v1/chat/stream",
            json=payload,
            headers=self.headers,
            stream=True,
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                for line in resp.iter_lines():
                    if line:
                        decoded = line.decode("utf-8")
                        if (
                            "event: final" in decoded
                            or "event: hitl_required" in decoded
                            or "event: error" in decoded
                        ):
                            break
                resp.success()
            elif resp.status_code == 429:
                resp.failure("Rate limited during the nominal release load profile")
            else:
                resp.failure(f"Stream failed with status: {resp.status_code}")

    @task(1)
    def test_health_check(self) -> None:
        """Pings readiness check endpoint."""
        self.client.get("/readyz")
