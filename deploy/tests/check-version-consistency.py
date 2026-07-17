#!/usr/bin/env python3
"""Fail when Arcturus product version metadata drifts across runtimes."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
RUST_PACKAGES = {"arcturus-auth", "arcturus-contracts", "arcturusd"}
NODE_MODULES = ("bus", "registry", "router")


def fail(message: str) -> None:
    raise SystemExit(f"version consistency check failed: {message}")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read {path.relative_to(ROOT)}: {error}")


def python_product_version(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        fail(f"cannot parse {path.relative_to(ROOT)}: {error}")
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "ARCTURUS_PRODUCT_VERSION" for target in statement.targets):
            continue
        if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            return statement.value.value
    fail("deploy/app.py does not define a literal ARCTURUS_PRODUCT_VERSION")


def assert_equal(label: str, actual: object, expected: str) -> None:
    if actual != expected:
        fail(f"{label} is {actual!r}; expected {expected!r}")


def main() -> int:
    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not expected or any(character.isspace() for character in expected):
        fail("VERSION must contain one non-empty token")

    compatibility = read_json(ROOT / "COMPATIBILITY.json")
    assert_equal("COMPATIBILITY.json productVersion", compatibility.get("productVersion"), expected)
    assert_equal(
        "deploy/app.py ARCTURUS_PRODUCT_VERSION",
        python_product_version(ROOT / "deploy/app.py"),
        expected,
    )

    rust_manifest = tomllib.loads((ROOT / "rust/Cargo.toml").read_text(encoding="utf-8"))
    assert_equal("rust workspace package version", rust_manifest["workspace"]["package"]["version"], expected)
    rust_toolchain = tomllib.loads((ROOT / "rust/rust-toolchain.toml").read_text(encoding="utf-8"))
    expected_rust = rust_toolchain["toolchain"]["channel"]
    assert_equal(
        "rust workspace minimum toolchain",
        rust_manifest["workspace"]["package"]["rust-version"],
        expected_rust,
    )
    for relative_path in (
        ".github/workflows/ci.yml",
        ".github/workflows/security.yml",
        "deploy/Containerfile.bundle",
    ):
        contents = (ROOT / relative_path).read_text(encoding="utf-8")
        if expected_rust not in contents:
            fail(f"{relative_path} does not reference pinned Rust {expected_rust}")
    rust_lock = tomllib.loads((ROOT / "rust/Cargo.lock").read_text(encoding="utf-8"))
    found = set()
    for package in rust_lock.get("package", []):
        name = package.get("name")
        if name not in RUST_PACKAGES:
            continue
        found.add(name)
        assert_equal(f"rust/Cargo.lock package {name}", package.get("version"), expected)
    missing = RUST_PACKAGES - found
    if missing:
        fail(f"rust/Cargo.lock is missing workspace packages: {', '.join(sorted(missing))}")

    for module in NODE_MODULES:
        package = read_json(ROOT / f"modules/{module}/package.json")
        lock = read_json(ROOT / f"modules/{module}/package-lock.json")
        assert_equal(f"modules/{module}/package.json version", package.get("version"), expected)
        assert_equal(f"modules/{module}/package-lock.json version", lock.get("version"), expected)
        root_package = lock.get("packages", {}).get("", {})
        assert_equal(
            f"modules/{module}/package-lock.json root package version",
            root_package.get("version"),
            expected,
        )

    print(f"Version metadata is consistent at {expected}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
