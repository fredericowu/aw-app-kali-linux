"""Release-image invariants: the published image must be multi-arch and the
manifest must pull from the registry owner build.yml actually pushes to."""
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = (ROOT / ".github/workflows/build.yml").read_text()
MANIFEST = json.loads((ROOT / "aw-app.json").read_text())


def test_release_platforms_include_arm64():
    # The default (push / release-bump) branch must publish both platforms.
    assert 'echo "platforms=linux/amd64,linux/arm64"' in BUILD
    # amd64-only is only the explicit opt-out on manual dispatch.
    assert "inputs.include_arm64 == false" in BUILD


def test_qemu_runs_unconditionally():
    m = re.search(r"- name: Set up QEMU\n(.*?)\n\n", BUILD, re.S)
    assert m and "if:" not in m.group(1)


def test_concurrency_never_cancels_in_flight_build():
    assert "cancel-in-progress: false" in BUILD


def test_manifest_image_owner_matches_build_push_target():
    # build.yml pushes to ghcr.io/<repository_owner lowercased>/aw-app-kali-linux
    assert "github.repository_owner" in BUILD
    remote = subprocess.run(
        ["git", "-C", str(ROOT), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    ).stdout
    image = MANIFEST["runtime"]["image"]
    assert image.startswith("ghcr.io/") and image.endswith("/aw-app-kali-linux:latest")
    owner = image.split("/")[1]
    assert owner == "fredericowu"
    if "github.com" in remote and "aw-app-kali-linux" in remote:
        assert f"/{owner}/" in remote.lower() or f":{owner}/" in remote.lower()
