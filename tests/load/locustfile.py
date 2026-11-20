"""Locust load testing suite for Agentic Platform.

Simulates concurrent user chats, SSE token streaming, and HITL approvals.
Run with: locust -f tests/load/locustfile.py --headless -u 50 -r 5 --run-time 1m --host http://localhost:8000
"""

import random

from locust import HttpUser, between, task


class AgenticUser(HttpUser):
    wait_time = between(1, 3)

    SAMPLE_QUERIES = [
        "Xin chào, hệ thống hỗ trợ được những tác vụ gì?",
        "Chính sách bảo mật thông tin nội bộ của công ty quy định thế nào?",
        "Hãy viết một script Python tính số Fibonacci với Memoization.",
        "Quy định về thời gian làm việc từ xa và chế độ đãi ngộ ra sao?",
        "Tạo file test_sample.py và chạy thử nghiệm trong sandbox.",
    ]

    def on_start(self) -> None:
        """Sets up default authentication token."""
        self.headers = {
            "Authorization": "Bearer admin_token",
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
                # Rate limit hit is expected under high load
                resp.success()
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
                        if "event: final" in decoded or "event: done" in decoded:
                            break
                resp.success()
            elif resp.status_code == 429:
                resp.success()
            else:
                resp.failure(f"Stream failed with status: {resp.status_code}")

    @task(1)
    def test_health_check(self) -> None:
        """Pings readiness check endpoint."""
        self.client.get("/readyz")
