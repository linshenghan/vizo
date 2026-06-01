#!/usr/bin/env python3
"""Sync the safe public subset of this repo into a separate Git repository."""

from __future__ import annotations

import argparse
import fnmatch
import glob
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MANIFEST = "PUBLIC_SYNC_MANIFEST.txt"
MAX_SCAN_BYTES = 2 * 1024 * 1024


SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9]{24,}"),
    re.compile(r"AT_[A-Za-z0-9]{20,}"),
    re.compile(r"UID_[A-Za-z0-9]{20,}"),
    re.compile(r"ms-[0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}"),
]


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Manifest:
    includes: list[str]
    excludes: list[str]


@dataclass
class SyncReport:
    copied: list[str]
    removed: list[str]
    kept: list[str]


def _to_posix(path: Path) -> str:
    return path.as_posix()


def load_manifest(path: Path) -> Manifest:
    includes: list[str] = []
    excludes: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            excludes.append(line[1:])
        else:
            includes.append(line)
    if not includes:
        raise SyncError(f"Manifest has no include rules: {path}")
    return Manifest(includes=includes, excludes=excludes)


def is_excluded(relative_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def collect_public_files(source: Path, manifest: Manifest) -> list[Path]:
    files: set[Path] = set()
    for pattern in manifest.includes:
        for match in glob.glob(str(source / pattern), recursive=True):
            candidate = Path(match)
            if not candidate.is_file():
                continue
            rel = _to_posix(candidate.relative_to(source))
            if not is_excluded(rel, manifest.excludes):
                files.add(candidate)
    return sorted(files, key=lambda item: _to_posix(item.relative_to(source)))


def assert_safe_target(source: Path, target: Path) -> None:
    source_resolved = source.resolve()
    target_resolved = target.resolve()
    if target_resolved == source_resolved:
        raise SyncError("Target repository cannot be the source repository.")
    if source_resolved in target_resolved.parents:
        raise SyncError("Target repository cannot be inside the source repository.")
    if target_resolved.anchor == str(target_resolved):
        raise SyncError("Refusing to sync into a filesystem root.")


def scan_for_secrets(paths: list[Path], source: Path) -> list[str]:
    findings: list[str] = []
    for path in paths:
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(_to_posix(path.relative_to(source)))
                break
    return findings


def _iter_target_files(target: Path) -> list[Path]:
    if not target.exists():
        return []
    return sorted(
        [path for path in target.rglob("*") if path.is_file()],
        key=lambda item: len(item.parts),
        reverse=True,
    )


def _is_git_path(relative_path: str) -> bool:
    return relative_path == ".git" or relative_path.startswith(".git/")


def sync_public_repo(
    source: Path,
    target: Path,
    manifest_path: Path,
    *,
    dry_run: bool = False,
    allow_secret_findings: bool = False,
    init_git: bool = False,
) -> SyncReport:
    source = source.resolve()
    manifest_path = manifest_path.resolve()
    assert_safe_target(source, target)
    manifest = load_manifest(manifest_path)
    public_files = collect_public_files(source, manifest)
    findings = scan_for_secrets(public_files, source)
    if findings and not allow_secret_findings:
        joined = "\n  - ".join(findings)
        raise SyncError(f"Possible live secret found in public files:\n  - {joined}")

    selected = {_to_posix(path.relative_to(source)) for path in public_files}
    copied: list[str] = []
    removed: list[str] = []
    kept: list[str] = []

    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)

    for src in public_files:
        rel = _to_posix(src.relative_to(source))
        dst = target / rel
        copied.append(rel)
        if dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    for dst in _iter_target_files(target):
        rel = _to_posix(dst.relative_to(target))
        if _is_git_path(rel):
            kept.append(rel)
            continue
        if rel not in selected:
            removed.append(rel)
            if not dry_run:
                dst.unlink()

    if not dry_run and target.exists():
        for directory in sorted(
            [path for path in target.rglob("*") if path.is_dir()],
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            rel = _to_posix(directory.relative_to(target))
            if _is_git_path(rel) or rel.startswith(".git/"):
                continue
            try:
                directory.rmdir()
            except OSError:
                pass

    if init_git and not dry_run and not (target / ".git").exists():
        subprocess.run(["git", "init"], cwd=target, check=True)

    return SyncReport(copied=copied, removed=removed, kept=kept)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(DEFAULT_MANIFEST))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--init-git", action="store_true")
    parser.add_argument(
        "--allow-secret-findings",
        action="store_true",
        help="Continue even if the lightweight scanner sees a likely live secret.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    source = args.source.resolve()
    manifest = args.manifest
    if not manifest.is_absolute():
        manifest = source / manifest
    try:
        report = sync_public_repo(
            source=source,
            target=args.target,
            manifest_path=manifest,
            dry_run=args.dry_run,
            allow_secret_findings=args.allow_secret_findings,
            init_git=args.init_git,
        )
    except (OSError, subprocess.CalledProcessError, SyncError) as exc:
        print(f"public sync failed: {exc}", file=sys.stderr)
        return 2

    mode = "dry run" if args.dry_run else "synced"
    print(f"public repo {mode}")
    print(f"copied: {len(report.copied)}")
    print(f"removed: {len(report.removed)}")
    if report.removed:
        print("removed files:")
        for rel in report.removed:
            print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
