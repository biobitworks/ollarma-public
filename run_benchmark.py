#!/usr/bin/env python3
"""Run full benchmark suite — standalone script for background execution."""
import os
import subprocess
import sys
import time
import traceback

os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"

# Ensure no models are loaded before starting
subprocess.run(["ollama", "stop", "qwen3:8b"], capture_output=True)
subprocess.run(["ollama", "stop", "qwen3:1.7b"], capture_output=True)
time.sleep(10)  # Wait for Ollama to fully unload

from ollarma.cli import app
from typer.testing import CliRunner

runner = CliRunner()
result = runner.invoke(app, [
    "run", "--trials", "1", "--skip-preflight",
    "--models", "deepseek-r1:8b",
    "--suites", "code",
])

with open("benchmark_output.log", "w") as f:
    f.write(result.output or "NO_OUTPUT\n")
    f.write(f"\nEXIT: {result.exit_code}\n")
    if result.exception:
        f.write("\n--- EXCEPTION ---\n")
        f.write("".join(traceback.format_exception(
            type(result.exception), result.exception,
            result.exception.__traceback__
        )))

print(f"Done. Exit code: {result.exit_code}")
print(f"Output written to benchmark_output.log")
