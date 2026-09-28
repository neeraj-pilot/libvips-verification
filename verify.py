import hashlib
import json
import math
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
inputs = Path("inputs")
inputs.mkdir(exist_ok=True)
results = []


def vips(*args):
    command = [str(binary), *map(str, args)]
    print(" ".join(command), flush=True)
    result = subprocess.run(command, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"exit {result.returncode}")
    return result.stdout.strip()


def dimensions(path):
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    offset = 2
    while offset < len(data):
        assert data[offset] == 255, "invalid JPEG marker"
        while data[offset] == 255:
            offset += 1
        marker = data[offset]
        offset += 1
        size = int.from_bytes(data[offset:offset + 2], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return width, height
        assert marker not in (0xDA, 0xD9), "missing JPEG dimensions"
        offset += size
    raise AssertionError("missing JPEG dimensions")


def check_jpeg(path, thumbnail=False):
    width, height = dimensions(path)
    if thumbnail:
        assert 0 < max(width, height) <= 720, (width, height)
    else:
        assert min(width, height) > 100, (width, height)
    deviation = float(vips("deviate", path))
    assert math.isfinite(deviation) and deviation > 1, deviation
    return dict(width=width, height=height, bytes=path.stat().st_size,
                deviation=deviation)


def check_hdr(path):
    decoded = output / "hdr-decoded.v"
    vips("uhdr2scRGB", path, decoded)
    maximum = float(vips("max", decoded))
    decoded.unlink()
    assert math.isfinite(maximum) and maximum > 1, maximum
    # Explicit jpegload exercises the backward-compatible SDR base image.
    base = output / "sdr-base.v"
    vips("jpegload", path, base)
    deviation = float(vips("deviate", base))
    base.unlink()
    assert deviation > 1, deviation
    return dict(hdr_max=maximum, sdr_deviation=deviation)


def record(name, test):
    try:
        entry = dict(name=name, status="pass", **test())
    except Exception as error:
        entry = dict(name=name, status="fail", error=str(error))
    results.append(entry)
    print(json.dumps(entry), flush=True)


def convert(source, name, hdr, reference=None):
    destination = output / f"{name}.jpeg"
    vips("copy", source, destination)
    info = check_jpeg(destination)
    if reference:
        assert dimensions(destination) == dimensions(reference), (
            "temporary input decoded a different-sized image",
            dimensions(destination), dimensions(reference))
    if hdr:
        info.update(check_hdr(destination))
    return info


def thumbnail(source, name, hdr, reference=None):
    info = {}
    for quality in (70, 60):
        destination = output / f"{name}-q{quality}.jpeg"
        vips("thumbnail", source, f"{destination}[Q={quality}]", 720,
             "--size", "down")
        current = check_jpeg(destination, thumbnail=True)
        if reference:
            expected = output / f"{reference}-q{quality}.jpeg"
            assert dimensions(destination) == dimensions(expected), (
                "temporary input returned a different-sized thumbnail",
                dimensions(destination), dimensions(expected))
        if hdr:
            current.update(check_hdr(destination))
        info[f"q{quality}"] = current
    return info


report = dict(
    source_run="https://github.com/ente/libvips-packaging/actions/runs/36383895870",
    source_commit="83260266f681aa6177b263d33ff207eb5c26084e",
    tag="v8.18.7", binary=binary.name,
    sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
    platform=platform.platform(), runner_arch=os.environ.get("RUNNER_ARCH"),
    version=vips("--version"), results=results,
)
assert "8.18.7" in report["version"], report["version"]
operations = vips("-l")
(output / "operations.txt").write_text(operations, encoding="utf-8")
for operation in ("dcrawload", "uhdrload", "uhdrsave", "uhdr2scRGB"):
    assert operation in operations, f"missing {operation}"

for fixture in json.loads(Path("fixtures.json").read_text()):
    source = inputs / fixture["name"]
    with urllib.request.urlopen(fixture["url"], timeout=90) as response:
        data = response.read()
    assert hashlib.sha256(data).hexdigest() == fixture["sha256"], fixture["name"]
    source.write_bytes(data)
    name = fixture["name"]
    hdr = fixture["kind"] == "hdr"
    record(f"{name}: thumbnail from path",
           lambda: thumbnail(source, name, hdr))
    if fixture["kind"] == "raw":
        record(f"{name}: full RAW decode with extension",
               lambda: convert(source, name, False))
    # The desktop conversion and archive/stream thumbnail paths use temporary
    # files without extensions. Test those separately from ordinary file paths.
    temporary = inputs / "temporary-input"
    shutil.copyfile(source, temporary)
    record(f"{name}: conversion from extensionless temporary file",
           lambda: convert(temporary, name + "-temporary", hdr,
                           output / f"{name}.jpeg"
                           if fixture["kind"] == "raw" else None))
    record(f"{name}: thumbnail from extensionless temporary file",
           lambda: thumbnail(temporary, name + "-temporary", hdr, name))

(output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
summary = ["| Check | Result |", "| --- | --- |"]
summary.extend(f"| {r['name']} | {r['status']} |" for r in results)
if "GITHUB_STEP_SUMMARY" in os.environ:
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text("\n".join(summary) + "\n")
sys.exit(any(result["status"] == "fail" for result in results))
