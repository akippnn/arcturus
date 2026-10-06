#!/usr/bin/env python3
"""Canonical Arcturus filesystem layout for Python, installers, and fixtures.

Environment variables ending in ``_ROOT`` name XDG/FHS roots. Variables ending
in ``_DIR`` name a final directory and are retained as compatibility overrides.
The Rust ``arcturus-paths`` crate implements the same contract and shares the
canonical fixture in ``rust/fixtures/paths``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shlex
from typing import Mapping


class PathResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ArcturusPaths:
    home: Path
    config_root: Path
    data_root: Path
    cache_root: Path
    runtime_root: Path
    config_dir: Path
    deployer_state_dir: Path
    fleet_state_dir: Path
    agent_state_dir: Path
    oci_auth_state_dir: Path
    oci_registry_state_dir: Path
    cache_dir: Path
    runtime_dir: Path
    systemd_dir: Path
    quadlet_dir: Path
    bin_dir: Path
    workload_root: Path

    def values(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


def _absolute(value: str | Path, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise PathResolutionError(f"{name} must be an absolute path: {path}")
    if any(character in str(path) for character in ("\n", "\r", "\0")):
        raise PathResolutionError(f"{name} contains a forbidden control character")
    return path


def _root(
    environment: Mapping[str, str], override: str, xdg: str, home: Path, fallback: str
) -> Path:
    value = environment.get(override) or environment.get(xdg)
    return _absolute(value or home / fallback, override)


def resolve_paths(
    environment: Mapping[str, str] = os.environ,
    *,
    home: str | Path | None = None,
    uid: int | None = None,
) -> ArcturusPaths:
    resolved_home = _absolute(home or environment.get("HOME") or Path.home(), "HOME")
    config_root = _root(environment, "ARCTURUS_CONFIG_ROOT", "XDG_CONFIG_HOME", resolved_home, ".config")
    data_root = _root(environment, "ARCTURUS_DATA_ROOT", "XDG_DATA_HOME", resolved_home, ".local/share")
    cache_root = _root(environment, "ARCTURUS_CACHE_ROOT", "XDG_CACHE_HOME", resolved_home, ".cache")
    runtime_base = environment.get("ARCTURUS_RUNTIME_ROOT") or environment.get("XDG_RUNTIME_DIR")
    if not runtime_base:
        runtime_base = f"/run/user/{uid if uid is not None else os.getuid()}"
    runtime_root = _absolute(runtime_base, "ARCTURUS_RUNTIME_ROOT")

    def final(name: str, fallback: Path) -> Path:
        return _absolute(environment.get(name) or fallback, name)

    return ArcturusPaths(
        home=resolved_home,
        config_root=config_root,
        data_root=data_root,
        cache_root=cache_root,
        runtime_root=runtime_root,
        config_dir=final("ARCTURUS_CONFIG_DIR", config_root / "arcturus"),
        deployer_state_dir=final("ARCTURUS_STATE_DIR", data_root / "arcturus-deployer"),
        fleet_state_dir=final("ARCTURUS_FLEET_STATE_DIR", data_root / "arcturus-fleet"),
        agent_state_dir=final("ARCTURUS_AGENT_STATE_DIR", data_root / "arcturus-agent"),
        oci_auth_state_dir=final("ARCTURUS_OCI_AUTH_STATE_DIR", data_root / "arcturus-oci-auth"),
        oci_registry_state_dir=final(
            "ARCTURUS_OCI_REGISTRY_STATE_DIR", data_root / "arcturus-registry"
        ),
        cache_dir=final("ARCTURUS_CACHE_DIR", cache_root / "arcturus"),
        runtime_dir=final("ARCTURUS_RUNTIME_DIR", runtime_root / "arcturus"),
        systemd_dir=final("ARCTURUS_SYSTEMD_DIR", config_root / "systemd/user"),
        quadlet_dir=final(
            "ARCTURUS_QUADLET_DIR", config_root / "containers/systemd/arcturus"
        ),
        bin_dir=final("ARCTURUS_BIN_DIR", resolved_home / ".local/bin"),
        workload_root=final("ARCTURUS_WORKLOAD_ROOT", resolved_home / "stacks"),
    )


PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")


def render_systemd_unit(template: str, values: Mapping[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        rendered = rendered.replace(f"@{key}@", escaped)
    unresolved = sorted(set(PLACEHOLDER.findall(rendered)))
    if unresolved:
        raise PathResolutionError(f"unresolved systemd placeholders: {', '.join(unresolved)}")
    return rendered


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Resolve the canonical Arcturus filesystem layout")
    result.add_argument("--home")
    result.add_argument("--uid", type=int)
    result.add_argument("--format", choices=("json", "shell"), default="json")
    result.add_argument("--get", choices=tuple(ArcturusPaths.__dataclass_fields__))
    result.add_argument("--render-unit", type=Path)
    result.add_argument("--output", type=Path)
    result.add_argument("--value", action="append", default=[], metavar="NAME=VALUE")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.render_unit:
        if not args.output:
            raise SystemExit("--render-unit requires --output")
        values: dict[str, str] = {}
        for item in args.value:
            key, separator, value = item.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise SystemExit("--value must use NAME=VALUE")
            values[key] = value
        args.output.write_text(
            render_systemd_unit(args.render_unit.read_text(encoding="utf-8"), values),
            encoding="utf-8",
        )
        return 0

    values = resolve_paths(home=args.home, uid=args.uid).values()
    if args.get:
        print(values[args.get])
        return 0
    if args.format == "json":
        print(json.dumps(values, indent=2, sort_keys=True))
    else:
        for key, value in values.items():
            print(f"ARCTURUS_PATH_{key.upper()}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
