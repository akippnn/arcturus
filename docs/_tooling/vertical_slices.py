#!/usr/bin/env python3
"""Automate mechanical evidence-first vertical-slice documentation work."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

try:
    import yaml
    from jsonschema import Draft202012Validator
except ModuleNotFoundError as error:  # pragma: no cover - exercised without the test environment
    project = Path(__file__).parent
    raise SystemExit(
        f"missing {error.name}; run this command through the pinned environment: "
        f"uv run --project {project} --frozen python {Path(__file__)} <arguments>"
    ) from error


TOOLING_VERSION = 1
CONFIG_SCHEMA = "delivery.documentation/v1"
BEGIN = "<!-- BEGIN GENERATED:{name} -->"
END = "<!-- END GENERATED:{name} -->"
STATE_VERDICTS = {
    "proposed": {"pending"},
    "ready": {"pending"},
    "active": {"pending"},
    "blocked": {"pending"},
    "audit-ready": {"pending"},
    "closed": {"accepted", "rejected", "pending"},
    "superseded": {"accepted", "rejected", "pending"},
    "deferred": {"pending"},
}
ACTIVE_STATES = {"ready", "active", "blocked", "audit-ready"}
LINK_PATTERN = re.compile(r"\[[^]]+\]\(([^)]+)\)")
MACHINE_PATTERNS = {
    "git-sha": re.compile(r"\b[0-9a-f]{40,}\b"),
    "iso-timestamp": re.compile(r"\b20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}"),
}


class DeliveryError(Exception):
    pass


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def relative_display(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return str(path)


def atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DeliveryError(f"{path}: cannot read YAML: {error}") from error
    if not isinstance(data, dict):
        raise DeliveryError(f"{path}: YAML root must be a mapping")
    return data


def split_frontmatter(repo: Path, path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise DeliveryError(f"{relative_display(repo, path)}: missing YAML frontmatter")
    try:
        _, raw, body = text.split("---\n", 2)
    except ValueError as error:
        raise DeliveryError(f"{relative_display(repo, path)}: unterminated YAML frontmatter") from error
    try:
        metadata = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise DeliveryError(f"{relative_display(repo, path)}: invalid YAML frontmatter: {error}") from error
    if not isinstance(metadata, dict):
        raise DeliveryError(f"{relative_display(repo, path)}: frontmatter must be a mapping")
    return metadata, body


def frontmatter_document(metadata: dict, body: str) -> str:
    header = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{header}\n---\n\n{body.rstrip()}\n"


def distribution_asset(name: str) -> Path:
    script = Path(__file__).resolve()
    candidate = script.parent.parent / "assets" / "project-tooling" / name
    if candidate.exists():
        return candidate
    candidate = script.parent / name
    if candidate.exists():
        return candidate
    raise DeliveryError(f"tooling asset is unavailable from this vendored copy: {name}")


def load_config(repo: Path, config_name: str) -> tuple[Path, dict]:
    path = (repo / config_name).resolve()
    if not path.exists():
        raise DeliveryError(f"missing configuration: {relative_display(repo, path)}")
    data = read_yaml(path)
    schema_path = distribution_asset("delivery-docs.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    failures = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda item: list(item.path))
    if failures:
        raise DeliveryError(f"{relative_display(repo, path)}: " + "; ".join(item.message for item in failures))
    targets = [item["target"] for item in data["projections"]]
    if len(targets) != len(set(targets)):
        raise DeliveryError(f"{relative_display(repo, path)}: projection targets must be unique")
    return path, data


def resolve(repo: Path, name: str) -> Path:
    path = (repo / name).resolve()
    try:
        path.relative_to(repo.resolve())
    except ValueError as error:
        raise DeliveryError(f"configured path escapes repository: {name}") from error
    return path


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def document_paths(repo: Path, config: dict) -> list[Path]:
    included: set[Path] = set()
    for pattern in config["documents"]["include"]:
        included.update(path for path in repo.glob(pattern) if path.is_file())
    excluded = config["documents"].get("exclude", [])
    return sorted(
        path.resolve()
        for path in included
        if not matches_any(path.relative_to(repo).as_posix(), excluded)
    )


def validate_schema(repo: Path, path: Path, data: dict, schema_name: str) -> None:
    schema_path = resolve(repo, schema_name)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    failures = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda item: list(item.path))
    if failures:
        raise DeliveryError(
            f"{relative_display(repo, path)}: " + "; ".join(item.message for item in failures)
        )


def load_documents(repo: Path, config: dict) -> dict[Path, tuple[dict, str]]:
    documents: dict[Path, tuple[dict, str]] = {}
    identifiers: dict[str, Path] = {}
    for path in document_paths(repo, config):
        metadata, body = split_frontmatter(repo, path)
        validate_schema(repo, path, metadata, config["documents"]["schema"])
        if metadata.get("lifecycle") == "generated" and not metadata.get("generated_by"):
            raise DeliveryError(f"{relative_display(repo, path)}: generated document needs generated_by")
        if metadata.get("generated_sections") and not metadata.get("generated_by"):
            raise DeliveryError(f"{relative_display(repo, path)}: generated sections need generated_by")
        for section in metadata.get("generated_sections", []):
            if BEGIN.format(name=section) not in body or END.format(name=section) not in body:
                raise DeliveryError(
                    f"{relative_display(repo, path)}: missing markers for generated section {section}"
                )
        if metadata.get("kind") == "contract":
            for key in ("slice", "version", "contract_state", "approval"):
                if key not in metadata:
                    raise DeliveryError(f"{relative_display(repo, path)}: contract needs {key}")
        if metadata.get("kind") == "evidence":
            for key in ("slice", "captured_at", "source", "gates"):
                if key not in metadata:
                    raise DeliveryError(f"{relative_display(repo, path)}: evidence needs {key}")
        identifier = metadata.get("id")
        if identifier:
            if identifier in identifiers:
                raise DeliveryError(
                    f"duplicate document id {identifier}: {relative_display(repo, identifiers[identifier])} "
                    f"and {relative_display(repo, path)}"
                )
            identifiers[identifier] = path
        documents[path] = (metadata, body)
    return documents


def relative_target(base: Path, target: str) -> Path:
    clean = unquote(target.split("#", 1)[0].strip("<>"))
    return (base.parent / clean).resolve()


def load_manifests(
    repo: Path, config: dict, documents: dict[Path, tuple[dict, str]]
) -> dict[str, tuple[Path, dict]]:
    root = resolve(repo, config["slices"]["root"])
    manifests: dict[str, tuple[Path, dict]] = {}
    for path in sorted(root.glob(f"*/{config['slices']['manifest']}")):
        data = read_yaml(path)
        validate_schema(repo, path, data, config["slices"]["schema"])
        slice_id = data["id"]
        if slice_id in manifests:
            raise DeliveryError(f"duplicate slice id {slice_id}")
        if data["verdict"] not in STATE_VERDICTS[data["state"]]:
            raise DeliveryError(f"{relative_display(repo, path)}: invalid state/verdict combination")
        if data["state"] == "blocked" and data["blocker"] is None:
            raise DeliveryError(f"{relative_display(repo, path)}: blocked slice needs a blocker")
        next_gate = data["next_gate"]
        if next_gate is not None and next_gate not in data["gates"]:
            raise DeliveryError(f"{relative_display(repo, path)}: next_gate is not declared")
        if next_gate is not None and data["gates"][next_gate]["state"] == "pass":
            raise DeliveryError(f"{relative_display(repo, path)}: next_gate already passes")
        contract = data["contract"]
        if (contract["file"] is None) != (contract["state"] == "not-applicable"):
            raise DeliveryError(f"{relative_display(repo, path)}: contract file/state disagree")
        if data["links"].get("contract") != contract["file"]:
            raise DeliveryError(f"{relative_display(repo, path)}: contract links disagree")
        references = [value for value in data["links"].values() if isinstance(value, str)]
        references.extend(data["links"].get("evidence", []))
        if contract["file"]:
            references.append(contract["file"])
        for gate_id, gate in data["gates"].items():
            if (gate["applicability"] == "not-applicable") != (gate["state"] == "not-applicable"):
                raise DeliveryError(f"{relative_display(repo, path)}: gate {gate_id} applicability/state disagree")
            if gate["state"] == "not-applicable" and gate["evidence"] is not None:
                raise DeliveryError(f"{relative_display(repo, path)}: inapplicable gate cannot cite evidence")
            if gate["state"] == "pass" and not gate["evidence"]:
                raise DeliveryError(f"{relative_display(repo, path)}: passing gate {gate_id} needs evidence")
            if gate["evidence"]:
                references.append(gate["evidence"])
        for reference in references:
            if not relative_target(path, reference).exists():
                raise DeliveryError(f"{relative_display(repo, path)}: missing reference {reference}")
        if contract["file"]:
            metadata = documents.get(relative_target(path, contract["file"]), ({}, ""))[0]
            if metadata.get("slice") != slice_id or str(metadata.get("version")) != str(contract["version"]):
                raise DeliveryError(f"{relative_display(repo, path)}: contract metadata disagrees")
        for gate_id, gate in data["gates"].items():
            if not gate["evidence"]:
                continue
            metadata = documents.get(relative_target(path, gate["evidence"]), ({}, ""))[0]
            if metadata.get("slice") != slice_id or gate_id not in metadata.get("gates", {}):
                raise DeliveryError(f"{relative_display(repo, path)}: evidence does not claim {gate_id}")
        manifests[slice_id] = (path, data)
    return manifests


def markdown_link(from_path: Path, to_path: Path, label: str) -> str:
    return f"[{label}]({Path(os.path.relpath(to_path, from_path.parent)).as_posix()})"


def replace_generated(text: str, name: str, content: str) -> str:
    begin = BEGIN.format(name=name)
    end = END.format(name=name)
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        raise DeliveryError(f"missing generated markers for {name}")
    return pattern.sub(f"{begin}\n{content.rstrip()}\n{end}", text)


def render_catalog(target: Path, documents: dict[Path, tuple[dict, str]]) -> str:
    sections: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for path, (metadata, _) in documents.items():
        nav = metadata.get("nav")
        if not nav or path == target:
            continue
        sections[nav["section"]].append(
            (nav["order"], f"- {markdown_link(target, path, metadata['title'])} — {metadata['summary']}")
        )
    lines: list[str] = []
    for section in sorted(sections, key=lambda item: min(order for order, _ in sections[item])):
        lines.extend([f"## {section}", ""])
        lines.extend(line for _, line in sorted(sections[section]))
        lines.append("")
    return "\n".join(lines).rstrip()


def json_pointer(value: object, pointer: str) -> object:
    current = value
    for segment in pointer.strip("/").split("/") if pointer else []:
        key = segment.replace("~1", "/").replace("~0", "~")
        current = current[int(key)] if isinstance(current, list) else current[key]  # type: ignore[index]
    return current


def render_fact(repo: Path, fact: dict) -> str:
    source = fact["source"]
    path = resolve(repo, source["file"])
    if source["format"] == "text":
        value: object = path.read_text(encoding="utf-8").strip()
    else:
        value = json_pointer(json.loads(path.read_text(encoding="utf-8")), source.get("pointer", ""))
    values = value if isinstance(value, list) else [value]
    rendered = ", ".join(f"`{item}`" if fact.get("code") else str(item) for item in values)
    return f"{fact['label']}: {rendered}"


def render_status(
    repo: Path, target: Path, projection: dict, manifests: dict[str, tuple[Path, dict]]
) -> str:
    lines = []
    facts = projection.get("facts", [])
    for index, fact in enumerate(facts):
        suffix = "  " if index < len(facts) - 1 else ""
        lines.append(render_fact(repo, fact) + suffix)
    if facts:
        lines.append("")
    lines.extend(
        [
            "| Slice | Outcome | State | Verdict | Next gate |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for slice_id, (path, data) in sorted(manifests.items()):
        overview = relative_target(path, data["links"]["overview"])
        lines.append(
            f"| {markdown_link(target, overview, slice_id)} | {data['title']} | `{data['state']}` | "
            f"`{data['verdict']}` | `{data['next_gate'] or '—'}` |"
        )
    lines.extend(["", "The slice manifest owns each row. Evidence owns the underlying results and limitations."])
    return "\n".join(lines)


def render_decisions(target: Path, documents: dict[Path, tuple[dict, str]]) -> str:
    rows = []
    for path, (metadata, _) in documents.items():
        if metadata.get("kind") == "adr":
            rows.append(
                (metadata["id"], markdown_link(target, path, metadata["title"]), metadata["status"], metadata["summary"])
            )
    lines = ["| ADR | Decision | Status | Summary |", "| --- | --- | --- | --- |"]
    lines.extend(f"| `{item}` | {link} | `{status}` | {summary} |" for item, link, status, summary in sorted(rows))
    return "\n".join(lines)


def render_slices(target: Path, manifests: dict[str, tuple[Path, dict]]) -> str:
    lines = ["| Slice | Outcome | State | Verdict |", "| --- | --- | --- | --- |"]
    for slice_id, (path, data) in sorted(manifests.items()):
        overview = relative_target(path, data["links"]["overview"])
        lines.append(
            f"| {markdown_link(target, overview, slice_id)} | {data['title']} | `{data['state']}` | `{data['verdict']}` |"
        )
    return "\n".join(lines)


def expected_outputs(
    repo: Path,
    config: dict,
    documents: dict[Path, tuple[dict, str]],
    manifests: dict[str, tuple[Path, dict]],
) -> dict[Path, str]:
    outputs = {}
    for projection in config["projections"]:
        target = resolve(repo, projection["target"])
        if target not in documents:
            raise DeliveryError(f"projection target is not a managed document: {relative_display(repo, target)}")
        metadata = documents[target][0]
        if projection["marker"] not in metadata.get("generated_sections", []):
            raise DeliveryError(
                f"{relative_display(repo, target)}: projection marker is not declared in generated_sections"
            )
        if metadata.get("lifecycle") in {"immutable", "archived"}:
            raise DeliveryError(f"{relative_display(repo, target)}: cannot generate into {metadata['lifecycle']} document")
        if projection["type"] == "catalog":
            content = render_catalog(target, documents)
        elif projection["type"] == "status":
            content = render_status(repo, target, projection, manifests)
        elif projection["type"] == "decisions":
            content = render_decisions(target, documents)
        else:
            content = render_slices(target, manifests)
        outputs[target] = replace_generated(target.read_text(encoding="utf-8"), projection["marker"], content)
    return outputs


def validate_links(repo: Path, documents: dict[Path, tuple[dict, str]]) -> None:
    broken = []
    for path, (_, body) in documents.items():
        for raw_target in LINK_PATTERN.findall(body):
            target = raw_target.strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#", "codex://", "thread://")):
                continue
            if not relative_target(path, target).exists():
                broken.append(f"{relative_display(repo, path)}: {raw_target}")
    if broken:
        raise DeliveryError("broken Markdown links:\n" + "\n".join(broken))


def validate_navigation(
    repo: Path, config: dict, documents: dict[Path, tuple[dict, str]], manifests: dict[str, tuple[Path, dict]]
) -> None:
    policy = config.get("navigation")
    if not policy:
        return
    graph: dict[Path, set[Path]] = {}
    for path, (_, body) in documents.items():
        targets = set()
        for raw_target in LINK_PATTERN.findall(body):
            target = raw_target.strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#", "codex://", "thread://")):
                continue
            resolved = relative_target(path, target)
            if resolved in documents:
                targets.add(resolved)
        graph[path] = targets
    start = resolve(repo, policy["start"])
    distances = {start: 0}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if distances[current] == policy["max_depth"]:
            continue
        for target in graph.get(current, set()):
            if target not in distances:
                distances[target] = distances[current] + 1
                queue.append(target)
    required = {resolve(repo, item) for item in policy["required"]}
    if policy["active_evidence"]:
        for manifest_path, data in manifests.values():
            if data["state"] in ACTIVE_STATES:
                required.update(relative_target(manifest_path, item) for item in data["links"].get("evidence", []))
    unreachable = sorted(relative_display(repo, item) for item in required if distances.get(item, 10**6) > policy["max_depth"])
    if unreachable:
        raise DeliveryError(
            f"{relative_display(repo, start)} must reach required destinations within "
            f"{policy['max_depth']} links: {', '.join(unreachable)}"
        )


def validate_prose(repo: Path, config: dict, documents: dict[Path, tuple[dict, str]]) -> None:
    for guard in config.get("prose_guards", []):
        for pattern in guard["paths"]:
            for path in repo.glob(pattern):
                resolved = path.resolve()
                if resolved not in documents:
                    continue
                body = documents[resolved][1]
                for name in guard["deny"]:
                    if MACHINE_PATTERNS[name].search(body):
                        raise DeliveryError(
                            f"{relative_display(repo, resolved)}: {name} belongs in structured metadata"
                        )


def validate_mermaid(repo: Path, config: dict) -> None:
    if not config.get("diagrams", {}).get("mermaid", False):
        return
    install = resolve(repo, config["tooling"]["install_dir"])
    validator = install / "validate-mermaid.mjs"
    if not (install / "node_modules").exists():
        raise DeliveryError(
            f"Mermaid dependencies are missing; run npm --prefix {relative_display(repo, install)} ci --no-audit --fund=false"
        )
    result = subprocess.run(
        ["node", str(validator), "--repo", str(repo)], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise DeliveryError((result.stderr or result.stdout).strip())


def validate_all(repo: Path, config: dict, *, require_fresh: bool, mermaid: bool = True) -> tuple[dict, dict, dict]:
    documents = load_documents(repo, config)
    manifests = load_manifests(repo, config, documents)
    outputs = expected_outputs(repo, config, documents, manifests)
    if require_fresh:
        stale = [relative_display(repo, path) for path, content in outputs.items() if path.read_text(encoding="utf-8") != content]
        if stale:
            raise DeliveryError("generated documentation is stale: " + ", ".join(stale))
    validate_links(repo, documents)
    validate_navigation(repo, config, documents, manifests)
    validate_prose(repo, config, documents)
    if mermaid:
        validate_mermaid(repo, config)
    return documents, manifests, outputs


def parse_pairs(values: list[str], *, allowed: set[str] | None = None) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise DeliveryError(f"expected NAME=VALUE: {value}")
        name, item = value.split("=", 1)
        if not name or not item or (allowed is not None and item not in allowed):
            raise DeliveryError(f"invalid NAME=VALUE: {value}")
        result[name] = item
    return result


def validate_manifest_mutation(repo: Path, config: dict, path: Path, data: dict) -> None:
    validate_schema(repo, path, data, config["slices"]["schema"])
    if data["verdict"] not in STATE_VERDICTS[data["state"]]:
        raise DeliveryError(f"{relative_display(repo, path)}: invalid state/verdict combination")
    if data["state"] == "blocked" and data["blocker"] is None:
        raise DeliveryError(f"{relative_display(repo, path)}: blocked slice needs a blocker")
    next_gate = data["next_gate"]
    if next_gate is not None and next_gate not in data["gates"]:
        raise DeliveryError(f"{relative_display(repo, path)}: next_gate is not declared")
    if next_gate is not None and data["gates"][next_gate]["state"] == "pass":
        raise DeliveryError(f"{relative_display(repo, path)}: next_gate already passes")
    for gate_id, gate in data["gates"].items():
        if (gate["applicability"] == "not-applicable") != (gate["state"] == "not-applicable"):
            raise DeliveryError(f"{relative_display(repo, path)}: gate {gate_id} applicability/state disagree")
        if gate["state"] == "not-applicable" and gate["evidence"] is not None:
            raise DeliveryError(f"{relative_display(repo, path)}: inapplicable gate cannot cite evidence")
        if gate["state"] == "pass" and not gate["evidence"]:
            raise DeliveryError(f"{relative_display(repo, path)}: passing gate {gate_id} needs evidence")


def git_output(repo: Path, arguments: list[str]) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, check=False)
    if result.returncode:
        raise DeliveryError(result.stderr.decode("utf-8", errors="replace").strip() or "Git command failed")
    return result.stdout


def repository_identity(repo: Path) -> dict:
    revision = git_output(repo, ["rev-parse", "HEAD"]).decode().strip()
    status = git_output(repo, ["status", "--porcelain=v2", "-z"])
    identity: dict[str, object] = {"repository": repo.name, "revision": revision, "state": "clean"}
    if not status:
        return identity
    patch = git_output(repo, ["diff", "--binary", "--full-index", "HEAD", "--"])
    untracked_raw = git_output(repo, ["ls-files", "--others", "--exclude-standard", "-z"])
    untracked = sorted(item for item in untracked_raw.split(b"\0") if item)
    untracked_digest = hashlib.sha256()
    for raw_name in untracked:
        path = repo / raw_name.decode("utf-8", errors="surrogateescape")
        untracked_digest.update(raw_name + b"\0")
        if path.is_symlink():
            untracked_digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            untracked_digest.update(bytes.fromhex(sha256_file(path).split(":", 1)[1]))
    combined = hashlib.sha256(patch)
    combined.update(untracked_digest.digest())
    identity.update(
        {
            "state": "dirty-working-copy",
            "working_tree_digest": "sha256:" + combined.hexdigest(),
            "tracked_patch_digest": sha256_bytes(patch),
            "untracked_manifest_digest": "sha256:" + untracked_digest.hexdigest(),
            "untracked_count": len(untracked),
        }
    )
    return identity


def plan_or_write(path: Path, content: str | bytes, plan: bool) -> None:
    if path.exists():
        raise DeliveryError(f"refusing to overwrite existing file: {path}")
    if plan:
        print(f"would create {path}")
    else:
        atomic_write(path, content)


def command_inspect(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    config_path = repo / args.config
    patterns = ["*.md", "docs/**/*.md", "portal/**/*.md", "runners/**/*.md"]
    excludes = [".git/**", ".github/**", "**/node_modules/**", "**/target/**"]
    config_error = None
    config = None
    if config_path.exists():
        try:
            _, config = load_config(repo, args.config)
            patterns = config["documents"]["include"]
            excludes = config["documents"].get("exclude", [])
        except DeliveryError as error:
            config_error = str(error)
    paths = set()
    for pattern in patterns:
        paths.update(path.resolve() for path in repo.glob(pattern) if path.is_file())
    paths = {path for path in paths if not matches_any(path.relative_to(repo).as_posix(), excludes)}
    missing = []
    authorities: dict[str, list[str]] = defaultdict(list)
    volatile = []
    for path in sorted(paths):
        try:
            metadata, body = split_frontmatter(repo, path)
            authorities[str(metadata.get("authority", ""))].append(relative_display(repo, path))
            if any(pattern.search(body) for pattern in MACHINE_PATTERNS.values()):
                volatile.append(relative_display(repo, path))
        except DeliveryError:
            missing.append(relative_display(repo, path))
    duplicate_authorities = {key: value for key, value in authorities.items() if key and len(value) > 1}
    slice_root = resolve(repo, config["slices"]["root"]) if config else repo / "docs" / "slices"
    manifest_name = config["slices"]["manifest"] if config else "manifest.yaml"
    slice_manifests = sorted(relative_display(repo, path) for path in slice_root.glob(f"*/{manifest_name}")) if slice_root.exists() else []
    legacy = sorted(
        relative_display(repo, path)
        for path in slice_root.glob("*.md")
        if re.search(r"-(contract|evidence|runbook)", path.name, re.IGNORECASE)
    ) if slice_root.exists() else []
    validation_error = None
    if config is not None:
        try:
            validate_all(repo, config, require_fresh=False, mermaid=False)
        except DeliveryError as error:
            validation_error = str(error)
    report = {
        "schema": "delivery.inspect/v1",
        "configured": config is not None,
        "config_error": config_error,
        "validation_error": validation_error,
        "documents": len(paths),
        "slice_manifests": slice_manifests,
        "missing_frontmatter": missing,
        "duplicate_authority_labels": duplicate_authorities,
        "volatile_prose_candidates": volatile,
        "legacy_slice_records": legacy,
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"documents={report['documents']} missing_frontmatter={len(missing)} "
            f"duplicate_authorities={len(duplicate_authorities)} volatile_candidates={len(volatile)} "
            f"slices={len(slice_manifests)} legacy_slice_records={len(legacy)} configured={report['configured']}"
        )
        if config_error:
            print(f"config: {config_error}")
        if validation_error:
            print(f"validation: {validation_error}")
        for authority, files in sorted(duplicate_authorities.items()):
            print(f"duplicate-authority: {authority}: {', '.join(files)}")
        for label, values in (("missing", missing), ("volatile", volatile), ("legacy", legacy)):
            for value in values:
                print(f"{label}: {value}")


DEFAULT_CONFIG = """schema: delivery.documentation/v1
tooling:
  install_dir: docs/_tooling
documents:
  include:
    - '*.md'
    - 'docs/**/*.md'
  exclude:
    - '.github/**'
    - 'docs/_tooling/node_modules/**'
  schema: docs/_tooling/document.schema.json
slices:
  root: docs/slices
  manifest: manifest.yaml
  schema: docs/_tooling/slice.schema.json
projections: []
navigation:
  start: docs/README.md
  max_depth: 2
  required: []
  active_evidence: true
prose_guards: []
diagrams:
  mermaid: false
"""


def vendor_sources(include_mermaid: bool) -> dict[str, Path]:
    sources = {
        "vertical_slices.py": Path(__file__).resolve(),
        "test_vertical_slices.py": Path(__file__).with_name("test_vertical_slices.py"),
        ".python-version": Path(__file__).with_name(".python-version"),
        "pyproject.toml": Path(__file__).with_name("pyproject.toml"),
        "uv.lock": Path(__file__).with_name("uv.lock"),
        "delivery-docs.schema.json": distribution_asset("delivery-docs.schema.json"),
    }
    if include_mermaid:
        sources.update(
            {
                "package.json": distribution_asset("package.json"),
                "package-lock.json": distribution_asset("package-lock.json"),
                "validate-mermaid.mjs": distribution_asset("validate-mermaid.mjs"),
                "test-mermaid.mjs": distribution_asset("test-mermaid.mjs"),
            }
        )
    return sources


def verify_vendor_targets(
    repo: Path, install_name: str, include_mermaid: bool = False
) -> tuple[Path, Path, dict, dict[str, Path]]:
    install = resolve(repo, install_name)
    manifest_path = install / ".vertical-slices-tooling.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"files": {}}
    sources = vendor_sources(include_mermaid)
    for name, source in sources.items():
        destination = install / name
        recorded = previous.get("files", {}).get(name)
        if destination.exists() and recorded is None:
            raise DeliveryError(f"refusing to replace unowned tooling file: {relative_display(repo, destination)}")
        if destination.exists() and recorded and sha256_file(destination) != recorded:
            raise DeliveryError(f"locally edited tool-owned file: {relative_display(repo, destination)}")
    for name, recorded in previous.get("files", {}).items():
        destination = install / name
        if name not in sources and destination.exists() and sha256_file(destination) != recorded:
            raise DeliveryError(f"locally edited obsolete tool-owned file: {relative_display(repo, destination)}")
    return install, manifest_path, previous, sources


def install_vendor(repo: Path, install_name: str, plan: bool, include_mermaid: bool = False) -> None:
    install, manifest_path, previous, sources = verify_vendor_targets(repo, install_name, include_mermaid)
    obsolete = [name for name in previous.get("files", {}) if name not in sources and (install / name).exists()]
    files = {name: sha256_file(source) for name, source in sources.items()}
    changed = [name for name, digest in files.items() if not (install / name).exists() or sha256_file(install / name) != digest]
    manifest_content = json.dumps(
        {"schema": "delivery.tooling/v1", "version": TOOLING_VERSION, "files": files}, indent=2, sort_keys=True
    ) + "\n"
    manifest_changed = not manifest_path.exists() or manifest_path.read_text(encoding="utf-8") != manifest_content
    if plan:
        for name in changed:
            print(f"would install {relative_display(repo, install / name)}")
        for name in obsolete:
            print(f"would remove obsolete {relative_display(repo, install / name)}")
        if manifest_changed:
            print(f"would record {relative_display(repo, manifest_path)}")
        print(f"tooling vendor planned; changed={len(changed) + len(obsolete) + int(manifest_changed)}")
        return
    install.mkdir(parents=True, exist_ok=True)
    for name in changed:
        source = sources[name]
        atomic_write(install / name, source.read_bytes())
    for name in obsolete:
        (install / name).unlink()
    if manifest_changed:
        atomic_write(manifest_path, manifest_content)
    print(f"tooling vendor passed; changed={len(changed) + len(obsolete) + int(manifest_changed)}")


def command_init_project(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    config_path = resolve(repo, args.config)
    install = resolve(repo, args.install_dir)
    targets = [config_path, install / "document.schema.json", install / "slice.schema.json"]
    existing = [relative_display(repo, path) for path in targets if path.exists()]
    if existing:
        raise DeliveryError("refusing to overwrite existing project files: " + ", ".join(existing))
    if args.vendor:
        verify_vendor_targets(repo, args.install_dir, False)
    plan_or_write(config_path, DEFAULT_CONFIG, args.plan)
    for name, target in zip(("document.schema.json", "slice.schema.json"), targets[1:]):
        plan_or_write(target, distribution_asset(name).read_bytes(), args.plan)
    if args.vendor:
        install_vendor(repo, args.install_dir, args.plan, False)


def command_vendor(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    _, config = load_config(repo, args.config)
    if args.install_dir:
        install_dir = args.install_dir
    else:
        install_dir = config["tooling"]["install_dir"]
    install_vendor(repo, install_dir, args.plan, config.get("diagrams", {}).get("mermaid", False))


def command_new_slice(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    _, config = load_config(repo, args.config)
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-[0-9]{3}", args.id):
        raise DeliveryError("slice id must match <UPPERCASE>-<three digits>")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.contract_version):
        raise DeliveryError("contract version must be a path-safe identifier")
    gates = parse_pairs(args.gate)
    root = resolve(repo, config["slices"]["root"]) / args.id
    if root.exists():
        raise DeliveryError(f"refusing to overwrite existing slice: {relative_display(repo, root)}")
    slice_schema = json.loads(resolve(repo, config["slices"]["schema"]).read_text(encoding="utf-8"))
    schema_id = slice_schema.get("properties", {}).get("schema", {}).get("const", "delivery.slice/v1")
    manifest = {
        "schema": schema_id,
        "id": args.id,
        "title": args.title,
        "state": "proposed",
        "verdict": "pending",
        "contract": {"version": args.contract_version, "state": "draft", "file": f"contract-{args.contract_version}.md"},
        "gates": {
            gate_id: {"applicability": "required", "state": "pending", "summary": summary, "evidence": None}
            for gate_id, summary in gates.items()
        },
        "blocker": None,
        "next_gate": next(iter(gates)),
        "links": {
            "overview": "README.md",
            "contract": f"contract-{args.contract_version}.md",
            "runbook": "runbook.md" if args.runbook else None,
            "evidence": [],
        },
    }
    validate_schema(repo, root / config["slices"]["manifest"], manifest, config["slices"]["schema"])
    overview_metadata = {
        "title": f"{args.id} {args.title}", "kind": "slice-overview", "lifecycle": "stable",
        "authority": f"Human overview of {args.id}", "summary": args.summary,
        "maintenance": ["The current contract pointer or explanatory diagram changes."],
        "id": f"{args.id}-overview", "slice": args.id,
    }
    overview_body = (
        f"# {args.id}: {args.title}\n\n{args.outcome}\n\n```mermaid\nflowchart LR\n"
        "  A[\"Authentic entrypoint\"] --> B[\"Product path\"]\n  B --> C[\"Observable result\"]\n```\n\n"
        "## Records\n\n- [Draft contract](contract-" + args.contract_version + ".md)\n"
        "- [Machine-readable state](manifest.yaml)\n" + ("- [Runbook](runbook.md)\n" if args.runbook else "")
    )
    contract_metadata = {
        "title": f"{args.id} contract", "kind": "contract", "lifecycle": "immutable",
        "authority": "Approved normative slice behavior", "summary": args.summary,
        "maintenance": ["Never edit after its immutable identity is recorded; create a new version."],
        "slice": args.id, "version": args.contract_version, "contract_state": "draft",
        "approval": {"authority": "pending", "record": "pending", "time": "pending", "immutable_identity": "pending"},
    }
    gate_rows = "\n".join(f"| `{gate}` | {summary} |" for gate, summary in gates.items())
    contract_body = (
        f"# {args.id}: {args.title}\n\n## Outcome\n\n{args.outcome}\n\n## Authentic path\n\n"
        "Describe the authentic entrypoint, product path, and observable result.\n\n## Required behavior\n\n"
        "Record only behavior approved for this outcome.\n\n## Failure and recovery\n\n"
        "Record the visible failure, safe behavior, and recovery path.\n\n## Excluded\n\n"
        "Name adjacent behavior outside this slice.\n\n## Gates\n\n| Gate | Required proof |\n| --- | --- |\n"
        f"{gate_rows}\n"
    )
    files = {
        root / config["slices"]["manifest"]: yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        root / "README.md": frontmatter_document(overview_metadata, overview_body),
        root / f"contract-{args.contract_version}.md": frontmatter_document(contract_metadata, contract_body),
    }
    if args.runbook:
        runbook_metadata = {
            "title": f"{args.id} runbook", "kind": "runbook", "lifecycle": "operational",
            "authority": f"{args.id} owner procedure", "summary": f"Exercise the {args.id} owner path.",
            "maintenance": ["Supported prerequisites, commands, safety, or recovery changes."], "slice": args.id,
        }
        files[root / "runbook.md"] = frontmatter_document(
            runbook_metadata,
            f"# {args.id} runbook\n\n## Before starting\n\nList prerequisites and safety boundaries.\n\n"
            "## Procedure\n\nExercise the authentic entrypoint and recovery behavior.\n\n"
            "## Record\n\nCreate a new immutable evidence record for the gates exercised.\n",
        )
    for path, content in files.items():
        if args.plan:
            print(f"would create {relative_display(repo, path)}")
        else:
            atomic_write(path, content)


def command_new_evidence(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    _, config = load_config(repo, args.config)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", args.name):
        raise DeliveryError("evidence name must be a lowercase path-safe identifier")
    documents = load_documents(repo, config)
    manifests = load_manifests(repo, config, documents)
    if args.slice not in manifests:
        raise DeliveryError(f"unknown slice: {args.slice}")
    manifest_path, manifest = manifests[args.slice]
    gate_results = parse_pairs(args.gate, allowed={"pass", "fail", "unavailable"})
    unknown = sorted(set(gate_results) - set(manifest["gates"]))
    if unknown:
        raise DeliveryError("evidence names unknown gates: " + ", ".join(unknown))
    evidence_path = manifest_path.parent / "evidence" / f"{args.name}.md"
    results = []
    for name in args.result_file:
        path = Path(name).resolve()
        results.append({"name": path.name, "digest": sha256_file(path), "bytes": path.stat().st_size})
    metadata = {
        "title": f"{args.slice} {args.name} evidence", "kind": "evidence", "lifecycle": "immutable",
        "authority": "Captured delivery evidence", "summary": args.summary,
        "maintenance": ["Never rewrite captured results; create a new record for a new attempt."],
        "slice": args.slice, "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": repository_identity(repo),
        "environment": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "results": results, "gates": gate_results,
    }
    body = (
        f"# {args.slice} evidence\n\n## Result\n\nSummarize the observable result.\n\n## Proves\n\n"
        "Name only the capability exercised by these inputs and environment.\n\n## Does not prove\n\n"
        "Name missing providers, packages, deployments, hardware, cohorts, or owner acceptance.\n\n"
        "## Findings and limitations\n\nRecord failures, unexpected behavior, and the smallest next action.\n"
    )
    plan_or_write(evidence_path, frontmatter_document(metadata, body), args.plan)


def command_reconcile(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    _, config = load_config(repo, args.config)
    documents = load_documents(repo, config)
    manifests = load_manifests(repo, config, documents)
    if args.slice not in manifests:
        raise DeliveryError(f"unknown slice: {args.slice}")
    manifest_path, manifest = manifests[args.slice]
    evidence_path = resolve(repo, args.evidence)
    evidence, _ = split_frontmatter(repo, evidence_path)
    validate_schema(repo, evidence_path, evidence, config["documents"]["schema"])
    if evidence.get("kind") != "evidence" or evidence.get("slice") != args.slice:
        raise DeliveryError("evidence metadata does not match the selected slice")
    gate_updates = parse_pairs(args.gate, allowed={"pass", "pending", "fail", "not-applicable"})
    evidence_reference = Path(os.path.relpath(evidence_path, manifest_path.parent)).as_posix()
    for gate_id, state in gate_updates.items():
        if gate_id not in manifest["gates"]:
            raise DeliveryError(f"unknown gate: {gate_id}")
        evidence_state = evidence.get("gates", {}).get(gate_id)
        if state in {"pass", "fail"} and evidence_state != state:
            raise DeliveryError(f"evidence does not claim {gate_id}={state}")
        manifest["gates"][gate_id]["state"] = state
        manifest["gates"][gate_id]["applicability"] = "not-applicable" if state == "not-applicable" else "required"
        manifest["gates"][gate_id]["evidence"] = evidence_reference if state in {"pass", "fail"} else None
    if evidence_reference not in manifest["links"].setdefault("evidence", []):
        manifest["links"]["evidence"].append(evidence_reference)
    if args.state:
        manifest["state"] = args.state
    if args.owner_verdict:
        manifest["verdict"] = args.owner_verdict
    if args.blocker is not None:
        manifest["blocker"] = None if args.blocker == "-" else args.blocker
    if args.next_gate is not None:
        manifest["next_gate"] = None if args.next_gate == "-" else args.next_gate
    validate_manifest_mutation(repo, config, manifest_path, manifest)
    content = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)
    if args.plan:
        print(f"would update {relative_display(repo, manifest_path)} from {relative_display(repo, evidence_path)}")
    else:
        atomic_write(manifest_path, content)
        refreshed_documents = load_documents(repo, config)
        load_manifests(repo, config, refreshed_documents)


def command_write(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    _, config = load_config(repo, args.config)
    _, _, outputs = validate_all(repo, config, require_fresh=False, mermaid=False)
    changed = [path for path, content in outputs.items() if path.read_text(encoding="utf-8") != content]
    for path in changed:
        if args.plan:
            print(f"would update {relative_display(repo, path)}")
        else:
            atomic_write(path, outputs[path])
    if not args.plan:
        validate_all(repo, config, require_fresh=True)
    print(f"documentation write {'planned' if args.plan else 'passed'}; changed={len(changed)}")


def command_check(args: argparse.Namespace) -> None:
    repo = args.repo.resolve()
    _, config = load_config(repo, args.config)
    documents, manifests, _ = validate_all(repo, config, require_fresh=True)
    print(f"documentation check passed; documents={len(documents)} slices={len(manifests)}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--repo", type=Path, default=Path.cwd())
    root.add_argument("--config", default="docs/delivery-docs.yaml")
    root.add_argument("--plan", action="store_true", help="show mutations without writing")
    commands = root.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--format", choices=("summary", "json"), default="summary")
    inspect.set_defaults(handler=command_inspect)

    initialize = commands.add_parser("init-project")
    initialize.add_argument("--install-dir", default="docs/_tooling")
    initialize.add_argument("--vendor", action="store_true")
    initialize.set_defaults(handler=command_init_project)

    vendor = commands.add_parser("vendor")
    vendor.add_argument("--install-dir")
    vendor.set_defaults(handler=command_vendor)

    create = commands.add_parser("new-slice")
    create.add_argument("--id", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--summary", required=True)
    create.add_argument("--outcome", required=True)
    create.add_argument("--gate", action="append", required=True, help="GATE-ID=required proof")
    create.add_argument("--contract-version", default="v1")
    create.add_argument("--runbook", action="store_true")
    create.set_defaults(handler=command_new_slice)

    evidence = commands.add_parser("new-evidence")
    evidence.add_argument("--slice", required=True)
    evidence.add_argument("--name", required=True)
    evidence.add_argument("--summary", required=True)
    evidence.add_argument("--gate", action="append", required=True, help="GATE-ID=pass|fail|unavailable")
    evidence.add_argument("--result-file", action="append", default=[])
    evidence.set_defaults(handler=command_new_evidence)

    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--slice", required=True)
    reconcile.add_argument("--evidence", required=True)
    reconcile.add_argument("--gate", action="append", default=[], help="GATE-ID=state")
    reconcile.add_argument("--state", choices=tuple(STATE_VERDICTS))
    reconcile.add_argument("--owner-verdict", choices=("pending", "accepted", "rejected"))
    reconcile.add_argument("--blocker", help="use - to clear")
    reconcile.add_argument("--next-gate", help="use - to clear")
    reconcile.set_defaults(handler=command_reconcile)

    write = commands.add_parser("write")
    write.set_defaults(handler=command_write)
    check = commands.add_parser("check")
    check.set_defaults(handler=command_check)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
    except (DeliveryError, OSError, KeyError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
