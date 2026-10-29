import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.integration.helpers import pack


@pytest.mark.parametrize("root_kind", ["default", "relative", "absolute", "path"])
@pytest.mark.parametrize("platform", [None, "ubuntu@24.04:amd64"])
def test_pack_returns_artifact_in_project_directory(tmp_path, monkeypatch, root_kind, platform):
    monkeypatch.chdir(tmp_path)
    project = tmp_path if root_kind == "default" else tmp_path / "tester"
    project.mkdir(exist_ok=True)
    roots = {"relative": "tester", "absolute": str(project), "path": project}
    filename = "tester_ubuntu@24.04-amd64.charm"

    def run(cmd, *, cwd, **kwargs):
        assert cmd == ["charmcraft", "pack"] + (["--platform", platform] if platform else [])
        assert cwd == project
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        (cwd / filename).touch()
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=f"Packed {filename}\n")

    with patch("tests.integration.helpers.subprocess.run", side_effect=run):
        charm = (
            pack(platform=platform)
            if root_kind == "default"
            else pack(roots[root_kind], platform=platform)
        )

    assert charm == project / filename
    assert charm.is_file()
    assert Path.cwd() == tmp_path


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        ("No artifacts produced\n", "Unable to get packed charm"),
        ("Packed first.charm\nPacked second.charm\n", "supports multiple platforms"),
    ],
)
def test_pack_rejects_missing_or_multiple_artifacts(tmp_path, stderr, message):
    result = subprocess.CompletedProcess(["charmcraft", "pack"], 0, stdout="", stderr=stderr)
    with patch("tests.integration.helpers.subprocess.run", return_value=result):
        with pytest.raises(ValueError, match=message):
            pack(tmp_path)
