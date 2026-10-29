from __future__ import annotations

import os
import subprocess
import sys

import pytest

from scripts import dev


@pytest.mark.parametrize("key", [None, "", "existing-key"])
def test_validate_supplies_key_only_to_children(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)
    calls = []
    monkeypatch.setattr(dev, "run", lambda *args, env=None: calls.append((args, env)))
    assert dev.main(["validate", "--config", "config with spaces/runtime.toml"]) == 0
    assert len(calls) == 3
    assert "config with spaces/runtime.toml" in calls[0][0]
    assert all(env["OPENROUTER_API_KEY"] == (key or "validate-only") for _, env in calls)
    assert os.environ.get("OPENROUTER_API_KEY") == key


def test_check_stops_on_failure_and_preserves_exit_code(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(7, args)

    monkeypatch.setattr(dev, "run", fail)
    assert dev.main(["check"]) == 7
    assert len(calls) == 1


def test_command_passes_unicode_and_spaces_without_shell_expansion(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    filename = "結果 with spaces.txt"
    text = "こんにちは $NOT_EXPANDED & value"
    dev.run(
        sys.executable,
        "-c",
        "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')",
        filename,
        text,
    )
    assert (tmp_path / filename).read_text(encoding="utf-8") == text
