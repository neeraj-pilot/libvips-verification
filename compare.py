import base64
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import urllib.request


binaries = {"old": Path(sys.argv[1]).resolve(),
            "new": Path(sys.argv[2]).resolve()}
root = Path("comparison")
root.mkdir(exist_ok=True)
inputs = Path("inputs")
inputs.mkdir(exist_ok=True)


def run(binary, *args):
    print(binary.name, *args, flush=True)
    result = subprocess.run([str(binary), *map(str, args)], capture_output=True,
                            text=True, timeout=180)
    return dict(exit_code=result.returncode, stdout=result.stdout.strip(),
                stderr=result.stderr.strip())


def required(binary, *args):
    result = run(binary, *args)
    if result["exit_code"]:
        raise RuntimeError(result["stderr"])
    return result["stdout"]


def dimensions(path):
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8", "not JPEG"
    offset = 2
    while offset < len(data):
        assert data[offset] == 255
        while data[offset] == 255:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return width, height
        assert marker not in (0xDA, 0xD9)
        offset += int.from_bytes(data[offset:offset + 2], "big")
    raise AssertionError("missing dimensions")


def hdr_probe(path):
    decoded = root / "hdr-check.v"
    result = run(binaries["new"], "uhdr2scRGB", path, decoded)
    info = dict(preserved=result["exit_code"] == 0, error=result["stderr"])
    if info["preserved"]:
        peak = float(required(binaries["new"], "max", decoded))
        assert math.isfinite(peak) and peak > 1, peak
        info["peak"] = peak
        decoded.unlink()
    return info


report = dict(platform=platform.platform(), arch=os.environ.get("RUNNER_ARCH"),
              source_run="https://github.com/ente/libvips-packaging/actions/runs/36383895870",
              old_release="https://github.com/ente/libvips-packaging/releases/tag/v8.16.0",
              binaries={}, fixtures=[])
for version, binary in binaries.items():
    if os.name != "nt":
        binary.chmod(0o755)
    operations = required(binary, "-l")
    (root / f"{version}-operations.txt").write_text(operations, encoding="utf-8")
    report["binaries"][version] = dict(
        name=binary.name, version=required(binary, "--version"),
        sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        operations={name: bool(re.search(r"\(" + name + r"\)", operations))
                    for name in ("dcrawload", "uhdrload", "uhdrsave", "uhdr2scRGB")})

for fixture in json.loads(Path("fixtures.json").read_text()):
    source = inputs / fixture["name"]
    with urllib.request.urlopen(fixture["url"], timeout=90) as response:
        data = response.read()
    assert hashlib.sha256(data).hexdigest() == fixture["sha256"]
    source.write_bytes(data)
    item = {**fixture, "results": {}}
    if fixture["kind"] == "hdr":
        item["source_hdr"] = hdr_probe(source)
        assert item["source_hdr"]["preserved"]
    for version, binary in binaries.items():
        directory = root / version
        directory.mkdir(exist_ok=True)
        item["results"][version] = {}
        for operation in ("copy", "thumbnail"):
            target = directory / f"{source.name}-{operation}.jpeg"
            args = (["copy", source, target] if operation == "copy" else
                    ["thumbnail", source, f"{target}[Q=70]", "720", "--size", "down"])
            result = run(binary, *args)
            result["command"] = list(map(str, args))
            if result["exit_code"] == 0:
                result.update(width=dimensions(target)[0], height=dimensions(target)[1],
                              file=str(target.relative_to(root)), bytes=target.stat().st_size)
                result["deviation"] = float(required(binaries["new"], "deviate", target))
                assert result["deviation"] > 1
                if fixture["kind"] == "hdr":
                    result["hdr"] = hdr_probe(target)
            item["results"][version][operation] = result
    report["fixtures"].append(item)

new_hdr = root / "new/ultra-hdr.jpg-thumbnail.jpeg"
native = root / "gainmap-metadata.v"
required(binaries["new"], "copy", new_hdr, native)
match = re.search(rb'<field\b[^>]*name="gainmap-data"[^>]*>(.*?)</field>',
                  native.read_bytes(), re.DOTALL)
assert match, "gain map missing from native metadata"
gainmap = root / "gainmap.jpeg"
gainmap.write_bytes(base64.b64decode(match.group(1)))
report["gainmap"] = dict(file=gainmap.name, dimensions=dimensions(gainmap))
native.unlink()
(root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report["binaries"], indent=2), flush=True)
