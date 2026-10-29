from __future__ import annotations

import pytest

from strands_runtime.errors import ReferenceResolutionError
from strands_runtime.factory.references import load_attribute


def test_load_attribute_from_native_absolute_path(tmp_path):
    directory = tmp_path / "共有 code"
    directory.mkdir()
    path = directory / "custom.py"
    path.write_text('def render():\n    return "こんにちは"\n', encoding="utf-8")
    render = load_attribute(f"{path}:render")
    assert render() == "こんにちは"
    assert load_attribute(f"{path}:render") is render


@pytest.mark.parametrize("reference", [r"C:\agents\custom.py", "pkg.module", "pkg.module:", ":attr"])
def test_attribute_is_required(reference):
    with pytest.raises(ReferenceResolutionError, match="module:attribute"):
        load_attribute(reference)
