import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import urllib.request


binary = Path(sys.argv[1]).resolve()
if os.name != "nt":
    binary.chmod(0o755)
output = Path("results")
output.mkdir(exist_ok=True)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dimensions(path):
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8"
    offset = 2
    while offset < len(data):
        assert data[offset] == 255
        while data[offset] == 255:
            offset += 1
        marker = data[offset]
        offset += 1
        size = int.from_bytes(data[offset:offset + 2], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return [width, height]
        assert marker not in (0xDA, 0xD9)
        offset += size
    raise AssertionError("Missing JPEG dimensions")


report = {
    "source_run": "https://github.com/ente/libvips-packaging/actions/runs/36383895870",
    "binary": binary.name,
    "binary_sha256": sha256(binary),
    "version": subprocess.check_output([str(binary), "--version"], text=True).strip(),
    "platform": platform.platform(),
    "arch": os.environ.get("RUNNER_ARCH"),
    "results": [],
}
assert "8.18.7" in report["version"]

for fixture in json.loads(Path("fixtures.json").read_text()):
    if fixture["name"] not in ("nikon.nef", "sony.arw"):
        continue
    source = output / fixture["name"]
    with urllib.request.urlopen(fixture["url"], timeout=90) as response:
        source.write_bytes(response.read())
    assert sha256(source) == fixture["sha256"]
    original_suffix = source.suffix
    baseline = {}
    for label, suffix in [
        ("original", original_suffix),
        ("uppercase", original_suffix.upper()),
        ("tif", ".tif"),
        ("tiff", ".tiff"),
        ("extensionless", ""),
    ]:
        directory = output / source.stem / label
        directory.mkdir(parents=True)
        renamed = directory / ("input" + suffix)
        shutil.copyfile(source, renamed)
        assert sha256(renamed) == fixture["sha256"]
        for operation in ("copy", "thumbnail"):
            destination = directory / (operation + ".jpeg")
            command = [str(binary), operation, str(renamed)]
            if operation == "copy":
                command.append(str(destination))
            else:
                command.extend([str(destination) + "[Q=70]", "720", "--size", "down"])
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
            (directory / (operation + "-stderr.txt")).write_text(result.stderr, encoding="utf-8")
            entry = {
                "fixture": fixture["name"], "fixture_sha256": fixture["sha256"],
                "variant": label, "suffix": suffix, "operation": operation,
                "command": command, "exit_code": result.returncode,
            }
            if result.returncode == 0:
                entry.update(dimensions=dimensions(destination), output_sha256=sha256(destination))
                if label == "original":
                    baseline[operation] = entry
                    if operation == "copy":
                        assert min(entry["dimensions"]) > 1000
                    else:
                        assert max(entry["dimensions"]) == 720
                entry["matches_original_dimensions"] = entry["dimensions"] == baseline[operation]["dimensions"]
                entry["matches_original_output"] = entry["output_sha256"] == baseline[operation]["output_sha256"]
            else:
                assert label != "original", "Original RAW conversion failed"
                entry["error"] = result.stderr.strip()
            report["results"].append(entry)
            print(json.dumps({k: v for k, v in entry.items() if k not in ("command", "error")}), flush=True)
        renamed.unlink()
    source.unlink()

(output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
summary = ["| Fixture | Input suffix | Operation | Result | Dimensions |", "| --- | --- | --- | --- | --- |"]
for entry in report["results"]:
    size = " × ".join(map(str, entry.get("dimensions", []))) or "—"
    outcome = "success" if entry["exit_code"] == 0 else "failed"
    summary.append(f"| {entry['fixture']} | {entry['suffix'] or '(none)'} | {entry['operation']} | {outcome} | {size} |")
if "GITHUB_STEP_SUMMARY" in os.environ:
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text("\n".join(summary) + "\n", encoding="utf-8")
