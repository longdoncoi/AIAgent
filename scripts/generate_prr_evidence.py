"""Generate machine-verifiable PRR evidence (no full test-suite execution).

Writes ``evidence/prr/latest.json`` with commit SHA, UTC timestamp, per-command
exit codes and captured output. The generated verdict is ALWAYS
``AUTO-EVIDENCE PENDING REVIEW BOARD`` — this script never assigns PASS/PASSED.
The Release Board must review the artifact and sign the 9 signature slots.

Commands executed (deliberately bounded — no full pytest run, because other
streams may be editing tests concurrently):

* ruff check
* mypy --no-incremental src/
* pytest --collect-only (collection only, no test execution)
* evals/run_eval.py --mode smoke
* YAML parse of all deploy/k8s manifests
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evidence" / "prr" / "latest.json"
PY = sys.executable

K8S_YAML_CHECK = (
    "import yaml,glob; "
    "[list(yaml.safe_load_all(open(f,encoding='utf-8'))) for f in glob.glob('deploy/k8s/*.yaml')]; "
    "yaml.safe_load(open('configs/litellm_config.yaml',encoding='utf-8')); "
    "yaml.safe_load(open('deploy/monitoring/alerts.yaml',encoding='utf-8'))"
)

CHECKS: list[tuple[str, list[str]]] = [
    ("ruff_check", [PY, "-m", "ruff", "check", "src/", "evals/", "tests/"]),
    ("mypy", [PY, "-m", "mypy", "--no-incremental", "src/"]),
    ("pytest_collect_only", [PY, "-m", "pytest", "tests/", "--collect-only", "-q"]),
    ("eval_smoke", [PY, "evals/run_eval.py", "--mode", "smoke"]),
    ("yaml_parse", [PY, "-c", K8S_YAML_CHECK]),
]


def _git_sha() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
        )
    except Exception:
        return "unknown"


def _run_check(name: str, command: list[str], timeout: int) -> dict[str, object]:
    env = dict(os.environ)
    env.setdefault("ENVIRONMENT", "test")
    env.setdefault("CI", "true")
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = "TIMEOUT after %ss" % timeout
    except Exception as exc:  # pragma: no cover - defensive
        exit_code = 126
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"

    return {
        "name": name,
        "command": command,
        "exit_code": exit_code,
        "passed": exit_code == 0,
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        nargs="*",
        choices=[name for name, _ in CHECKS],
        help="Run only the selected checks (default: all).",
    )
    parser.add_argument("--timeout", type=int, default=600, help="Per-check timeout in seconds.")
    args = parser.parse_args()

    selected = [(n, c) for n, c in CHECKS if not args.only or n in args.only]
    results = [_run_check(name, command, args.timeout) for name, command in selected]
    all_passed = all(bool(r["passed"]) for r in results)

    payload = {
        "schema_version": "1.0",
        "artifact_type": "prr_auto_evidence",
        "verdict": "AUTO-EVIDENCE PENDING REVIEW BOARD",
        "production_ready": False,
        "commit_sha": _git_sha(),
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "python": sys.version,
        "checks": results,
        "all_checks_passed": all_passed,
        "notes": (
            "No PASS/PASSED verdict is assigned by automation. "
            "Nine (9) signature slots remain empty pending Release Board sign-off. "
            "See .workflows/reviews/REVIEW.md section 6.3."
        ),
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[PRR] evidence written to {OUTPUT.relative_to(ROOT)}")
    print(f"[PRR] commit={payload['commit_sha']} all_checks_passed={all_passed}")
    for result in results:
        print(f"  {result['name']:<20} exit={result['exit_code']}")
    return 0  # evidence capture itself succeeded; failures are recorded in JSON


if __name__ == "__main__":
    raise SystemExit(main())
