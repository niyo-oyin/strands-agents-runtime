"""Windows / macOS / Linux 共通の開発コマンド。``uv run python scripts/dev.py check`` で実行する。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = (
    "check",
    "lint",
    "typecheck",
    "test",
    "validate",
    "integ",
    "docker-build",
    "docker-run",
    "hooks",
    "synth",
    "phoenix-up",
    "phoenix-down",
)


def run(*command: str, env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(command), flush=True)
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONUTF8", "1")
    subprocess.run(command, cwd=ROOT, env=child_env, check=True)


def python(*args: str, env: dict[str, str] | None = None) -> None:
    run(sys.executable, "-m", *args, env=env)


def stop_phoenix() -> None:
    subprocess.run(
        ["docker", "rm", "-f", "strands-phoenix"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def execute(task: str, *, config: str, image: str) -> None:
    match task:
        case "check":
            for step in ("lint", "typecheck", "test", "validate"):
                execute(step, config=config, image=image)
        case "lint":
            python("ruff", "format", "--check", "src", "tests", "tests_integ", "infra", "scripts")
            python("ruff", "check", "src", "tests", "tests_integ", "infra", "scripts")
        case "typecheck":
            python("mypy")
        case "test":
            python("pytest", "-q")
        case "validate":
            env = dict(os.environ)
            if not env.get("OPENROUTER_API_KEY"):
                env["OPENROUTER_API_KEY"] = "validate-only"
            python("strands_runtime.cli", "-c", config, "validate", env=env)
            for example in ("feature-check", "reference"):
                python("strands_runtime.cli", "-c", f"examples/{example}/runtime.toml", "validate", "--mcp", env=env)
        case "integ":
            run("uv", "run", "--env-file", ".env", "pytest", "tests_integ", "-q")
        case "docker-build":
            run("docker", "buildx", "build", "--platform", "linux/arm64", "-t", image, "--load", ".")
        case "docker-run":
            run("docker", "run", "--rm", "-p", "8080:8080", "--env-file", ".env", "-e", "OTEL_SDK_DISABLED=true", image)
        case "hooks":
            python("pre_commit", "install", "--hook-type", "pre-commit", "--hook-type", "pre-push")
        case "synth":
            python("infra.app")
        case "phoenix-up":
            stop_phoenix()
            run(
                "docker",
                "run",
                "-d",
                "--name",
                "strands-phoenix",
                "-p",
                "6006:6006",
                "-p",
                "4317:4317",
                "arizephoenix/phoenix:latest",
            )
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen("http://localhost:6006/healthz", timeout=2):
                        print("Phoenix: http://localhost:6006")
                        return
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(1)
            raise RuntimeError("Phoenix did not become ready within 90 seconds")
        case "phoenix-down":
            stop_phoenix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=("help", *TASKS), nargs="?", default="help")
    parser.add_argument("--config", default="runtime.toml")
    parser.add_argument("--image", default="strands-runtime:dev")
    args = parser.parse_args(argv)
    if args.task == "help":
        parser.print_help()
        return 0
    try:
        execute(args.task, config=args.config, image=args.image)
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
