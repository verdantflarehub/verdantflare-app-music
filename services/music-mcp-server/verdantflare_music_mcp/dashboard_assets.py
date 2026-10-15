"""Deterministic App-owned Dashboard package and verified, bounded asset reads for Music MCP."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from . import __version__


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode(data: bytes | str) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("INVALID_DASHBOARD_ARTIFACT")
            value[key] = item
        return value

    return json.loads(data, object_pairs_hook=unique)


def build(source: Path | str, output: Path | str, version: str = __version__) -> dict:
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("INVALID_APP_VERSION")
    source_path, output_path = Path(source), Path(output)
    if output_path.exists() and any(output_path.iterdir()):
        raise ValueError("OUTPUT_NOT_EMPTY")
    files: dict[str, bytes] = {}

    def asset(name: str, data: bytes) -> str:
        original = Path(name)
        path = f"assets/{original.stem}.{hashlib.sha256(data).hexdigest()[:24]}{original.suffix}"
        files[path] = data
        return path

    css_text = (source_path / "dashboard.css").read_text(encoding="utf-8").replace("\r\n", "\n")
    css = asset("dashboard.css", css_text.encode("utf-8"))
    code = (source_path / "dashboard.mjs").read_text(encoding="utf-8").replace("\r\n", "\n")
    entry = asset("dashboard.mjs", code.encode("utf-8"))
    html = (source_path / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    if html.count("./dashboard.css") != 1 or html.count("./dashboard.mjs") != 1:
        raise ValueError("INVALID_DASHBOARD_ENTRY")
    files["index.html"] = (
        html.replace("./dashboard.css", "./" + css)
        .replace("./dashboard.mjs", "./" + entry)
        .encode("utf-8")
    )
    index = canonical(
        {
            "schemaVersion": 1,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        }
    )
    declaration = {
        "schemaVersion": 1,
        "appId": "music-mcp",
        "appVersion": version,
        "dashboard": {
            "entryRef": "dashboard/index.html",
            "artifactDigest": "sha256:" + hashlib.sha256(index).hexdigest(),
            "adapter": "wujie",
            "sdkMajor": 1,
            "minimumSdkVersion": "1.2.0",
            "requiredCapabilities": ["app.request", "navigation.request"],
            "optionalCapabilities": ["artifact.read", "navigation.openApp"],
            "appApiPolicyRef": "music-mcp-dashboard-v1",
            "keepAlive": True,
        },
    }
    output_path.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = output_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (output_path / "asset-index.json").write_bytes(index)
    (output_path / "dashboard-declaration.json").write_bytes(canonical(declaration))
    DashboardAssets(output_path, version=version)
    return declaration


class DashboardAssets:
    TYPES = {
        ".html": "text/html; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
    }

    def __init__(self, root: Path | str, *, version: str = __version__) -> None:
        self.root = Path(root).resolve()
        self.declaration = self._read("dashboard-declaration.json", 65536)
        self.index = self._read("asset-index.json", 65536)
        declaration, index = decode(self.declaration), decode(self.index)
        if (
            not isinstance(declaration, dict)
            or set(declaration) != {"schemaVersion", "appId", "appVersion", "dashboard"}
            or type(declaration["schemaVersion"]) is not int
            or declaration["schemaVersion"] != 1
            or declaration["appId"] != "music-mcp"
            or declaration["appVersion"] != version
        ):
            raise ValueError("INVALID_DASHBOARD_ARTIFACT")
        dashboard = declaration["dashboard"]
        if (
            not isinstance(dashboard, dict)
            or dashboard.get("adapter") != "wujie"
            or dashboard.get("entryRef") != "dashboard/index.html"
            or dashboard.get("appApiPolicyRef") != "music-mcp-dashboard-v1"
            or dashboard.get("artifactDigest") != "sha256:" + hashlib.sha256(self.index).hexdigest()
            or not isinstance(index, dict)
            or set(index) != {"schemaVersion", "files"}
            or type(index["schemaVersion"]) is not int
            or index["schemaVersion"] != 1
            or not isinstance(index["files"], dict)
            or not 1 <= len(index["files"]) <= 256
            or "index.html" not in index["files"]
        ):
            raise ValueError("INVALID_DASHBOARD_ARTIFACT")
        self.files: dict[str, str] = dict(index["files"])
        for name, digest in self.files.items():
            if (
                not self.safe(name)
                or Path(name).suffix not in self.TYPES
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
            ):
                raise ValueError("INVALID_DASHBOARD_ARTIFACT")
            self.asset(name)

    @staticmethod
    def safe(name: str) -> bool:
        return (
            isinstance(name, str)
            and len(name) <= 256
            and bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]*", name))
            and all(part and not part.startswith(".") for part in name.split("/"))
        )

    def _read(self, name: str, maximum: int = 4 << 20) -> bytes:
        if not self.safe(name):
            raise ValueError("INVALID_DASHBOARD_PATH")
        target = self.root / name
        if (
            any(path.is_symlink() for path in [target, *target.parents] if path != self.root)
            or not target.resolve().is_relative_to(self.root)
            or not target.is_file()
            or target.stat().st_size > maximum
        ):
            raise ValueError("DASHBOARD_ASSET_UNAVAILABLE")
        with target.open("rb") as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("DASHBOARD_ASSET_UNAVAILABLE")
        return data

    def asset(self, name: str) -> tuple[bytes, str]:
        if name not in self.files:
            raise KeyError("NOT_FOUND")
        data = self._read(name)
        if hashlib.sha256(data).hexdigest() != self.files[name]:
            raise ValueError("DASHBOARD_ASSET_MISMATCH")
        return data, self.TYPES[Path(name).suffix]


def load_or_build_assets(version: str = __version__) -> DashboardAssets | None:
    configured = Path(os.environ.get("MUSIC_DASHBOARD_DIR", "/app/dashboard"))
    if configured.is_dir() and (configured / "dashboard-declaration.json").is_file():
        return DashboardAssets(configured, version=version)
    source = Path(__file__).resolve().parent / "dashboard_src"
    if source.is_dir() and (source / "index.html").is_file():
        tmp_dir = Path(tempfile.mkdtemp(prefix="music-dashboard-build-"))
        build(source, tmp_dir, version=version)
        return DashboardAssets(tmp_dir, version=version)
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.output, __version__)
