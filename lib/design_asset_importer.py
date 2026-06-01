"""Project-scoped design asset importer for Vizo generated frontends.

The public entry point is :func:`import_design_assets_for_project`.  It imports
locally referenced design assets into the given project root and records a
manifest even when optional npm-distributed assets cannot be fetched.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


FONT_PACKAGES: dict[str, dict[str, Any]] = {
    "@fontsource/inter": {
        "key": "inter",
        "family": "Inter",
        "aliases": ["inter"],
        "weights": [400, 500, 600, 700, 800],
        "subsets": ["latin"],
    },
    "@fontsource/jetbrains-mono": {
        "key": "jetbrains-mono",
        "family": "JetBrains Mono",
        "aliases": ["jetbrains mono", "jetbrains-mono", "jetbrainsmono"],
        "weights": [400, 500, 600, 700],
        "subsets": ["latin"],
    },
    "@fontsource/ibm-plex-sans": {
        "key": "ibm-plex-sans",
        "family": "IBM Plex Sans",
        "aliases": ["ibm plex sans", "ibm-plex-sans", "ibmplexsans"],
        "weights": [400, 500, 600, 700],
        "subsets": ["latin"],
    },
    "@fontsource/plus-jakarta-sans": {
        "key": "plus-jakarta-sans",
        "family": "Plus Jakarta Sans",
        "aliases": ["plus jakarta sans", "plus-jakarta-sans", "plusjakartasans"],
        "weights": [400, 500, 600, 700, 800],
        "subsets": ["latin"],
    },
    "@fontsource/space-grotesk": {
        "key": "space-grotesk",
        "family": "Space Grotesk",
        "aliases": ["space grotesk", "space-grotesk", "spacegrotesk"],
        "weights": [400, 500, 600, 700],
        "subsets": ["latin"],
    },
    "@fontsource/sora": {
        "key": "sora",
        "family": "Sora",
        "aliases": ["sora"],
        "weights": [400, 500, 600, 700, 800],
        "subsets": ["latin"],
    },
    "@fontsource/dm-serif-display": {
        "key": "dm-serif-display",
        "family": "DM Serif Display",
        "aliases": ["dm serif display", "dm-serif-display", "dmserifdisplay"],
        "weights": [400],
        "subsets": ["latin"],
    },
    "@fontsource/noto-serif-sc": {
        "key": "noto-serif-sc",
        "family": "Noto Serif SC",
        "aliases": ["noto serif sc", "noto-serif-sc", "notoserifsc"],
        "weights": [400, 500, 600, 700],
        "subsets": ["chinese-simplified", "latin"],
    },
}

FONT_ALIAS_TO_PACKAGE = {
    alias: package
    for package, metadata in FONT_PACKAGES.items()
    for alias in metadata["aliases"]
}

ICON_PACKAGES = {
    "material-symbols": "material-symbols",
    "material-design-icons-svg": "@material-design-icons/svg",
}

MATERIAL_SYMBOL_STYLES = ["outlined", "rounded", "sharp"]
DEFAULT_ICON_NAMES = [
    "add",
    "arrow_back",
    "arrow_forward",
    "check",
    "chevron_left",
    "chevron_right",
    "close",
    "delete",
    "download",
    "edit",
    "expand_more",
    "home",
    "menu",
    "more_vert",
    "play_arrow",
    "refresh",
    "search",
    "settings",
    "star",
    "upload",
]


@dataclass(frozen=True)
class Paths:
    project_root: Path
    manifest: Path
    lock: Path
    fonts: Path
    vendor: Path
    google_fonts_css: Path
    material_symbols_css: Path
    icons: Path
    app_css: Path
    memory: Path


class AssetImportError(RuntimeError):
    """Raised for a single asset failure that should be recorded in manifest."""


def import_design_assets_for_project(
    project_root: Path | str,
    candidate: dict[str, Any],
    *,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Import locally hosted design assets for a generated frontend project.

    All files are written below ``project_root``.  Failures to fetch npm
    packages are recorded on the affected resource, and the function still
    returns and writes the best-effort manifest.
    """

    root = Path(project_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths = _paths_for(root)
    requested = _requested_resources(candidate)

    paths.fonts.mkdir(parents=True, exist_ok=True)
    paths.vendor.mkdir(parents=True, exist_ok=True)
    paths.icons.mkdir(parents=True, exist_ok=True)
    paths.app_css.parent.mkdir(parents=True, exist_ok=True)
    paths.manifest.parent.mkdir(parents=True, exist_ok=True)
    paths.memory.parent.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now(timezone.utc).isoformat()
    resources: list[dict[str, Any]] = []
    font_css_blocks: list[str] = [_generated_header("google font CSS", generated_at)]
    symbol_css_blocks: list[str] = [_generated_header("Material Symbols CSS", generated_at)]

    with tempfile.TemporaryDirectory(prefix="vizo-design-assets-") as tmp:
        tmp_dir = Path(tmp)
        for font_request in requested["fonts"]:
            resource, css = _import_font_resource(font_request, paths, tmp_dir, replace_existing)
            resources.append(resource)
            if css:
                font_css_blocks.append(css.strip())

        if requested["material_symbols"]["enabled"]:
            resource, css = _import_material_symbols_resource(
                requested["material_symbols"],
                paths,
                tmp_dir,
                replace_existing,
            )
            resources.append(resource)
            if css:
                symbol_css_blocks.append(css.strip())

        if requested["material_svg"]["enabled"]:
            resources.append(
                _import_material_svg_resource(
                    requested["material_svg"],
                    paths,
                    tmp_dir,
                    replace_existing,
                )
            )
        else:
            resources.append(
                {
                    "type": "icon-svg",
                    "id": ICON_PACKAGES["material-design-icons-svg"],
                    "status": "skipped",
                    "reason": requested["material_svg"].get("reason", "SVG icon import disabled by resource declaration."),
                    "files": [],
                }
            )

    _write_text(paths.google_fonts_css, "\n\n".join(font_css_blocks).rstrip() + "\n", replace_existing=True)
    _write_text(paths.material_symbols_css, "\n\n".join(symbol_css_blocks).rstrip() + "\n", replace_existing=True)
    app_css = _build_app_css(candidate, requested, paths, generated_at)
    _write_text(paths.app_css, app_css, replace_existing=True)
    _sync_html_fallback_assets(paths)

    resources.append(
        {
            "type": "motion",
            "id": "vizo-local-motion-tokens",
            "status": "imported",
            "reason": "Generated local CSS motion tokens; no external animation library is required.",
            "files": [_rel(root, paths.app_css)],
        }
    )

    manifest = _build_manifest(root, paths, candidate, resources, requested, replace_existing, generated_at)
    _write_json(paths.manifest, manifest)
    _write_json(paths.lock, _build_lock(root, manifest, paths))
    _write_text(paths.memory, _build_memory_markdown(manifest), replace_existing=True)
    return manifest


def _paths_for(root: Path) -> Paths:
    app_css = root / "src" / "styles" / "vizo-design.css"
    if not ((root / "src").exists() or (root / "package.json").exists()):
        app_css = root / "assets" / "vizo-design" / "vizo-design.css"

    return Paths(
        project_root=root,
        manifest=root / ".vizo" / "design-assets.json",
        lock=root / ".vizo" / "design-assets.lock",
        fonts=root / "public" / "vizo-design" / "fonts" / "google",
        vendor=root / "public" / "vizo-design" / "vendor",
        google_fonts_css=root / "public" / "vizo-design" / "vendor" / "google-fonts.css",
        material_symbols_css=root / "public" / "vizo-design" / "vendor" / "material-symbols.css",
        icons=root / "public" / "vizo-design" / "icons" / "material-design",
        app_css=app_css,
        memory=root / ".serena" / "memories" / "design" / "assets.md",
    )


def _requested_resources(candidate: dict[str, Any]) -> dict[str, Any]:
    resources = candidate.get("resources")
    fonts: list[dict[str, Any]] = []
    material_symbols = {"enabled": True, "styles": MATERIAL_SYMBOL_STYLES}
    material_svg = {"enabled": True, "styles": ["filled"], "icons": DEFAULT_ICON_NAMES}

    if resources:
        parsed = _parse_resources(resources)
        fonts = parsed["fonts"]
        material_symbols = parsed["material_symbols"]
        material_svg = parsed["material_svg"]

    if not fonts:
        inferred = _infer_font_packages(candidate)
        fonts = [{"type": "font", "package": package, "source": "inferred"} for package in inferred]

    if not fonts:
        fonts = [{"type": "font", "package": "@fontsource/inter", "source": "fallback"}]

    return {
        "fonts": _dedupe_font_requests(fonts),
        "material_symbols": material_symbols,
        "material_svg": material_svg,
    }


def _parse_resources(resources: Any) -> dict[str, Any]:
    fonts: list[dict[str, Any]] = []
    material_symbols = {"enabled": False, "styles": MATERIAL_SYMBOL_STYLES}
    material_svg = {"enabled": False, "styles": ["filled"], "icons": DEFAULT_ICON_NAMES}

    entries: list[Any] = []
    if isinstance(resources, list):
        entries = resources
    elif isinstance(resources, dict):
        for item in _as_list(resources.get("fonts") or resources.get("font")):
            entries.append({"type": "font", **item} if isinstance(item, dict) else {"type": "font", "package": item})
        for item in _as_list(resources.get("icons") or resources.get("icon")):
            entries.append({"type": "icon", **item} if isinstance(item, dict) else {"type": "icon", "package": item})
        if "motion" in resources:
            entries.append({"type": "motion", "strategy": resources.get("motion")})
        for item in resources.get("assets", []) if isinstance(resources.get("assets"), list) else []:
            entries.append(item)

    for raw in entries:
        entry = raw if isinstance(raw, dict) else {"package": raw}
        package = str(entry.get("package") or entry.get("id") or "").strip()
        resource_type = str(entry.get("type") or entry.get("kind") or "").lower()
        descriptor = _normalize_name(
            " ".join(
                str(entry.get(key) or "")
                for key in ("package", "id", "source", "name", "family")
            )
        )
        if package in FONT_PACKAGES or resource_type in {"font", "fonts", "typography"}:
            normalized_package = _normalize_font_package(
                package
                or str(entry.get("family") or "")
                or str(entry.get("name") or "")
            )
            if normalized_package:
                fonts.append({**entry, "type": "font", "package": normalized_package})
            continue

        if (
            package == ICON_PACKAGES["material-symbols"]
            or resource_type in {"material-symbols", "icon-font"}
            or "material symbols" in descriptor
            or "material_symbols" in descriptor
        ):
            material_symbols = {
                "enabled": not _is_disabled(entry),
                "styles": _styles_from_entry(entry, MATERIAL_SYMBOL_STYLES),
            }
            continue

        if (
            package == ICON_PACKAGES["material-design-icons-svg"]
            or resource_type in {"icon-svg", "svg-icons"}
            or "material design icons svg" in descriptor
            or "material_design_icons" in descriptor
        ):
            include_svg = entry.get("include_svg", entry.get("includeSvg", entry.get("svg", True)))
            enabled = (not _is_disabled(entry)) and bool(include_svg)
            material_svg = {
                "enabled": enabled,
                "styles": _styles_from_entry(entry, ["filled"]),
                "icons": _icons_from_entry(entry),
            }
            if not enabled:
                material_svg["reason"] = "SVG icon import disabled by resource declaration."
            continue

    return {
        "fonts": fonts,
        "material_symbols": material_symbols,
        "material_svg": material_svg,
    }


def _infer_font_packages(candidate: dict[str, Any]) -> list[str]:
    ui_tokens = candidate.get("ui_tokens") if isinstance(candidate.get("ui_tokens"), dict) else {}
    font_values = [
        ui_tokens.get("font_family"),
        ui_tokens.get("fontFamily"),
        ui_tokens.get("font_sans"),
        ui_tokens.get("fontSans"),
        ui_tokens.get("heading_font"),
        ui_tokens.get("headingFont"),
        candidate.get("font_family"),
    ]
    packages: list[str] = []
    for value in font_values:
        for family in _split_font_families(value):
            package = _normalize_font_package(family)
            if package and package not in packages:
                packages.append(package)
    return packages


def _normalize_font_package(value: str) -> str | None:
    normalized = _normalize_name(value)
    if not normalized:
        return None
    if value in FONT_PACKAGES:
        return value
    return FONT_ALIAS_TO_PACKAGE.get(normalized)


def _split_font_families(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = list(value.values())
    else:
        values = re.split(r"[,/]", str(value))
    return [str(item).strip().strip("\"'") for item in values if str(item).strip()]


def _dedupe_font_requests(fonts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for font in fonts:
        package = _normalize_font_package(str(font.get("package") or font.get("name") or ""))
        if not package or package in seen:
            continue
        seen.add(package)
        deduped.append({**font, "package": package})
    return deduped


def _import_font_resource(
    request: dict[str, Any],
    paths: Paths,
    tmp_dir: Path,
    replace_existing: bool,
) -> tuple[dict[str, Any], str]:
    package = request["package"]
    metadata = FONT_PACKAGES[package]
    resource = {
        "type": "font",
        "id": package,
        "family": metadata["family"],
        "status": "pending",
        "source": request.get("source", "candidate.resources"),
        "files": [],
        "css_files": [],
    }
    try:
        archive = _npm_pack(package, tmp_dir)
        css_blocks, files, css_files = _extract_fontsource_css(archive, request, metadata, paths, replace_existing)
    except Exception as exc:  # noqa: BLE001 - resource failures must be captured.
        resource["status"] = "failed"
        resource["reason"] = _reason(exc)
        return resource, ""

    resource["status"] = "imported" if files or css_blocks else "pending"
    resource["reason"] = "Imported from npm package." if files else "No matching CSS/font files were found in package."
    resource["files"] = [_rel(paths.project_root, path) for path in files]
    resource["css_files"] = css_files
    return resource, "\n\n".join(css_blocks)


def _extract_fontsource_css(
    archive: Path,
    request: dict[str, Any],
    metadata: dict[str, Any],
    paths: Paths,
    replace_existing: bool,
) -> tuple[list[str], list[Path], list[str]]:
    desired = _desired_font_css_files(archive, request, metadata)
    css_blocks: list[str] = []
    files: list[Path] = []
    css_files: list[str] = []
    with tarfile.open(archive, "r:gz") as tar:
        names = {member.name for member in tar.getmembers() if member.isfile()}
        for css_name in desired:
            if css_name not in names:
                continue
            css = _extract_text_from_tar(tar, css_name)
            rewritten, extracted_files = _rewrite_font_urls(
                css,
                tar,
                css_name,
                str(metadata["key"]),
                paths,
                replace_existing,
            )
            css_blocks.append(f"/* {metadata['family']} - {PurePosixPath(css_name).name} */\n{rewritten.strip()}")
            css_files.append(PurePosixPath(css_name).name)
            files.extend(extracted_files)
    return css_blocks, files, css_files


def _desired_font_css_files(archive: Path, request: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    weights = [int(weight) for weight in _as_list(request.get("weights"))] or list(metadata["weights"])
    subsets = [str(subset) for subset in _as_list(request.get("subsets"))] or list(metadata["subsets"])
    candidates: list[str] = []
    for subset in subsets:
        for weight in weights:
            candidates.extend(
                [
                    f"package/{subset}-{weight}.css",
                    f"package/{subset}-{weight}-normal.css",
                    f"package/{weight}.css",
                    f"package/{weight}-normal.css",
                ]
            )

    with tarfile.open(archive, "r:gz") as tar:
        names = [member.name for member in tar.getmembers() if member.isfile() and member.name.endswith(".css")]

    selected = [name for name in candidates if name in names]
    if selected:
        return _unique(selected)

    weight_pattern = "|".join(str(weight) for weight in weights)
    subset_pattern = "|".join(re.escape(subset) for subset in subsets)
    fallback = [
        name
        for name in names
        if re.search(rf"(^|/|-)({weight_pattern})(-|\.css$)", name)
        and (not subset_pattern or re.search(rf"({subset_pattern})", name))
    ]
    if fallback:
        return _unique(fallback[: len(weights) * max(1, len(subsets))])
    return ["package/index.css"] if "package/index.css" in names else []


def _rewrite_font_urls(
    css: str,
    tar: tarfile.TarFile,
    css_member_name: str,
    local_prefix: str,
    paths: Paths,
    replace_existing: bool,
) -> tuple[str, list[Path]]:
    extracted: list[Path] = []
    css_parent = PurePosixPath(css_member_name).parent

    def replace(match: re.Match[str]) -> str:
        raw_source = match.group(1).strip().strip("\"'")
        if raw_source.startswith(("http://", "https://")):
            raise AssetImportError(f"Remote font URL is not allowed in generated design assets: {raw_source}")
        if raw_source.startswith("data:"):
            return match.group(0)
        member_name = _safe_posix_join(css_parent, raw_source)
        file_name = PurePosixPath(raw_source).name
        local_name = f"{local_prefix}-{file_name}"
        target = paths.fonts / local_name
        _extract_member_from_tar(tar, member_name, target, replace_existing)
        extracted.append(target)
        return f"url('/vizo-design/fonts/google/{local_name}')"

    return re.sub(r"url\(([^)]+)\)", replace, css), extracted


def _import_material_symbols_resource(
    request: dict[str, Any],
    paths: Paths,
    tmp_dir: Path,
    replace_existing: bool,
) -> tuple[dict[str, Any], str]:
    resource = {
        "type": "icon-font",
        "id": ICON_PACKAGES["material-symbols"],
        "status": "pending",
        "styles": request.get("styles", MATERIAL_SYMBOL_STYLES),
        "files": [],
        "css_files": [],
    }
    try:
        archive = _npm_pack(ICON_PACKAGES["material-symbols"], tmp_dir)
        css_blocks: list[str] = []
        files: list[Path] = []
        css_files: list[str] = []
        with tarfile.open(archive, "r:gz") as tar:
            names = {member.name for member in tar.getmembers() if member.isfile()}
            for style in request.get("styles", MATERIAL_SYMBOL_STYLES):
                font_member = f"package/material-symbols-{style}.woff2"
                css_member = f"package/{style}.css"
                if font_member not in names or css_member not in names:
                    continue
                font_name = PurePosixPath(font_member).name
                target = paths.fonts / font_name
                _extract_member_from_tar(tar, font_member, target, replace_existing)
                css = _extract_text_from_tar(tar, css_member)
                css = css.replace(f'url("./{font_name}")', f"url('/vizo-design/fonts/google/{font_name}')")
                css = css.replace(f"url(./{font_name})", f"url('/vizo-design/fonts/google/{font_name}')")
                css_blocks.append(f"/* Material Symbols {style} */\n{css.strip()}")
                files.append(target)
                css_files.append(PurePosixPath(css_member).name)
    except Exception as exc:  # noqa: BLE001
        resource["status"] = "failed"
        resource["reason"] = _reason(exc)
        return resource, ""

    resource["status"] = "imported" if files else "pending"
    resource["reason"] = "Imported from npm package." if files else "No matching Material Symbols files were found."
    resource["files"] = [_rel(paths.project_root, path) for path in files]
    resource["css_files"] = css_files
    return resource, "\n\n".join(css_blocks)


def _import_material_svg_resource(
    request: dict[str, Any],
    paths: Paths,
    tmp_dir: Path,
    replace_existing: bool,
) -> dict[str, Any]:
    package = ICON_PACKAGES["material-design-icons-svg"]
    resource = {
        "type": "icon-svg",
        "id": package,
        "status": "pending",
        "styles": request.get("styles", ["filled"]),
        "requested_icons": request.get("icons", DEFAULT_ICON_NAMES),
        "files": [],
    }
    try:
        archive = _npm_pack(package, tmp_dir)
        files: list[Path] = []
        with tarfile.open(archive, "r:gz") as tar:
            names = [member.name for member in tar.getmembers() if member.isfile() and member.name.endswith(".svg")]
            selected = _select_svg_members(names, request.get("styles", ["filled"]), request.get("icons", DEFAULT_ICON_NAMES))
            for member_name in selected:
                relative = PurePosixPath(member_name).relative_to("package")
                target = paths.icons / Path(*relative.parts)
                _extract_member_from_tar(tar, member_name, target, replace_existing)
                files.append(target)
    except Exception as exc:  # noqa: BLE001
        resource["status"] = "failed"
        resource["reason"] = _reason(exc)
        return resource

    resource["status"] = "imported" if files else "pending"
    resource["reason"] = "Imported selected SVG icons from npm package." if files else "No matching SVG icons were found."
    resource["files"] = [_rel(paths.project_root, path) for path in files]
    resource["count"] = len(files)
    return resource


def _select_svg_members(names: list[str], styles: list[str], icons: list[str]) -> list[str]:
    style_set = {_normalize_icon_style(style) for style in styles}
    icon_set = {str(icon).strip().replace("-", "_") for icon in icons if str(icon).strip()}
    selected: list[str] = []
    for name in names:
        path = PurePosixPath(name)
        stem = path.stem
        parts = set(path.parts)
        if icon_set and stem not in icon_set:
            continue
        if style_set and not any(style in parts for style in style_set):
            continue
        selected.append(name)
    return selected


def _build_app_css(candidate: dict[str, Any], requested: dict[str, Any], paths: Paths, generated_at: str) -> str:
    families = [FONT_PACKAGES[request["package"]]["family"] for request in requested["fonts"] if request["package"] in FONT_PACKAGES]
    primary_font = families[0] if families else "Inter"
    mono_font = "JetBrains Mono" if "JetBrains Mono" in families else "ui-monospace"
    motion = _motion_tokens(candidate)
    if _is_html_fallback_css(paths):
        imports = [
            "@import './vendor/google-fonts.css';",
            "@import './vendor/material-symbols.css';",
        ]
    else:
        imports = [
            "@import '/vizo-design/vendor/google-fonts.css';",
            "@import '/vizo-design/vendor/material-symbols.css';",
        ]
    return "\n".join(
        [
            _generated_header("project design CSS", generated_at).strip(),
            *imports,
            "",
            ":root {",
            f"  --vizo-font-sans: \"{primary_font}\", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif;",
            f"  --vizo-font-mono: \"{mono_font}\", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", monospace;",
            f"  --vizo-motion-duration-fast: {motion['duration_fast']};",
            f"  --vizo-motion-duration-base: {motion['duration_base']};",
            f"  --vizo-motion-duration-slow: {motion['duration_slow']};",
            f"  --vizo-motion-ease-standard: {motion['ease_standard']};",
            f"  --vizo-motion-ease-emphasized: {motion['ease_emphasized']};",
            "}",
            "",
            ".vizo-design-fonts {",
            "  font-family: var(--vizo-font-sans);",
            "}",
            "",
            ".vizo-motion-standard {",
            "  transition-duration: var(--vizo-motion-duration-base);",
            "  transition-timing-function: var(--vizo-motion-ease-standard);",
            "}",
            "",
            ".vizo-material-symbol {",
            "  font-family: 'Material Symbols Rounded', 'Material Symbols Outlined', sans-serif;",
            "  font-weight: normal;",
            "  font-style: normal;",
            "  font-size: 1.25em;",
            "  line-height: 1;",
            "  display: inline-block;",
            "  white-space: nowrap;",
            "  direction: ltr;",
            "  -webkit-font-feature-settings: 'liga';",
            "  -webkit-font-smoothing: antialiased;",
            "  font-feature-settings: 'liga';",
            "}",
            "",
            "@media (prefers-reduced-motion: reduce) {",
            "  :root {",
            "    --vizo-motion-duration-fast: 1ms;",
            "    --vizo-motion-duration-base: 1ms;",
            "    --vizo-motion-duration-slow: 1ms;",
            "  }",
            "",
            "  *,",
            "  *::before,",
            "  *::after {",
            "    animation-duration: 1ms !important;",
            "    animation-iteration-count: 1 !important;",
            "    scroll-behavior: auto !important;",
            "    transition-duration: 1ms !important;",
            "  }",
            "}",
            "",
        ]
    )


def _sync_html_fallback_assets(paths: Paths) -> None:
    if not _is_html_fallback_css(paths):
        return
    fallback_root = paths.app_css.parent
    fallback_vendor = fallback_root / "vendor"
    fallback_fonts = fallback_root / "fonts" / "google"
    fallback_icons = fallback_root / "icons" / "material-design"
    fallback_vendor.mkdir(parents=True, exist_ok=True)
    if paths.fonts.exists():
        shutil.copytree(paths.fonts, fallback_fonts, dirs_exist_ok=True)
    if paths.icons.exists():
        shutil.copytree(paths.icons, fallback_icons, dirs_exist_ok=True)
    for css_path in (paths.google_fonts_css, paths.material_symbols_css):
        if not css_path.exists():
            continue
        css = css_path.read_text(encoding="utf-8")
        css = css.replace("/vizo-design/fonts/google/", "../fonts/google/")
        (fallback_vendor / css_path.name).write_text(css, encoding="utf-8")


def _is_html_fallback_css(paths: Paths) -> bool:
    return _rel(paths.project_root, paths.app_css).startswith("assets/vizo-design/")


def _html_fallback_usage(root: Path, paths: Paths) -> dict[str, str]:
    if _is_html_fallback_css(paths):
        fallback_root = paths.app_css.parent
        return {
            "app_css": _rel(root, paths.app_css),
            "google_fonts_css": _rel(root, fallback_root / "vendor" / "google-fonts.css"),
            "material_symbols_css": _rel(root, fallback_root / "vendor" / "material-symbols.css"),
            "fonts": _rel(root, fallback_root / "fonts" / "google"),
            "icons": _rel(root, fallback_root / "icons" / "material-design"),
        }
    return {
        "app_css": _rel(root, paths.app_css),
        "google_fonts_css": _rel(root, paths.google_fonts_css),
        "material_symbols_css": _rel(root, paths.material_symbols_css),
        "fonts": _rel(root, paths.fonts),
        "icons": _rel(root, paths.icons),
    }


def _motion_tokens(candidate: dict[str, Any]) -> dict[str, str]:
    resources = candidate.get("resources") if isinstance(candidate.get("resources"), dict) else {}
    motion = resources.get("motion", {}) if isinstance(resources, dict) else {}
    if not isinstance(motion, dict):
        motion = {"strategy": motion}
    strategy = str(motion.get("strategy") or candidate.get("motion_strategy") or "balanced").lower()
    if strategy in {"reduced", "calm", "minimal"}:
        return {
            "duration_fast": "120ms",
            "duration_base": "160ms",
            "duration_slow": "220ms",
            "ease_standard": "cubic-bezier(0.2, 0, 0, 1)",
            "ease_emphasized": "cubic-bezier(0.2, 0, 0, 1)",
        }
    if strategy in {"expressive", "playful", "energetic"}:
        return {
            "duration_fast": "160ms",
            "duration_base": "260ms",
            "duration_slow": "420ms",
            "ease_standard": "cubic-bezier(0.2, 0, 0, 1)",
            "ease_emphasized": "cubic-bezier(0.05, 0.7, 0.1, 1)",
        }
    return {
        "duration_fast": "140ms",
        "duration_base": "220ms",
        "duration_slow": "320ms",
        "ease_standard": "cubic-bezier(0.2, 0, 0, 1)",
        "ease_emphasized": "cubic-bezier(0.05, 0.7, 0.1, 1)",
    }


def _build_manifest(
    root: Path,
    paths: Paths,
    candidate: dict[str, Any],
    resources: list[dict[str, Any]],
    requested: dict[str, Any],
    replace_existing: bool,
    generated_at: str,
) -> dict[str, Any]:
    summary = {"imported": 0, "pending": 0, "failed": 0, "skipped": 0}
    for resource in resources:
        status = str(resource.get("status", "pending"))
        summary[status] = summary.get(status, 0) + 1
    blocking_count = int(summary.get("failed", 0)) + int(summary.get("pending", 0))
    imported_count = int(summary.get("imported", 0))
    if blocking_count == 0 and imported_count > 0:
        status = "imported"
        success = True
    elif imported_count > 0:
        status = "partial"
        success = False
    elif int(summary.get("failed", 0)) > 0:
        status = "failed"
        success = False
    elif int(summary.get("pending", 0)) > 0:
        status = "pending"
        success = False
    else:
        status = "skipped"
        success = False

    return {
        "schema_version": 1,
        "status": status,
        "success": success,
        "generated_at": generated_at,
        "project_root": str(root),
        "replace_existing": replace_existing,
        "candidate": _candidate_summary(candidate),
        "paths": {
            "manifest": _rel(root, paths.manifest),
            "lock": _rel(root, paths.lock),
            "fonts": _rel(root, paths.fonts),
            "google_fonts_css": _rel(root, paths.google_fonts_css),
            "material_symbols_css": _rel(root, paths.material_symbols_css),
            "material_design_icons": _rel(root, paths.icons),
            "app_css": _rel(root, paths.app_css),
            "memory": _rel(root, paths.memory),
        },
        "usage": {
            "css_import": _rel(root, paths.app_css),
            "public_css": [
                "/vizo-design/vendor/google-fonts.css",
                "/vizo-design/vendor/material-symbols.css",
            ],
            "html_fallback": _html_fallback_usage(root, paths),
        },
        "css": {
            "imports": [_rel(root, paths.app_css)],
            "public_imports": [
                "/vizo-design/vendor/google-fonts.css",
                "/vizo-design/vendor/material-symbols.css",
            ],
        },
        "requested": {
            "fonts": [request["package"] for request in requested["fonts"]],
            "material_symbols": requested["material_symbols"],
            "material_svg": requested["material_svg"],
        },
        "resources": resources,
        "summary": summary,
    }


def _build_lock(root: Path, manifest: dict[str, Any], paths: Paths) -> dict[str, Any]:
    tracked = [
        paths.google_fonts_css,
        paths.material_symbols_css,
        paths.app_css,
        paths.memory,
    ]
    files = []
    for resource in manifest["resources"]:
        for relative in resource.get("files", []):
            tracked.append(root / relative)
    if _is_html_fallback_css(paths) and paths.app_css.parent.exists():
        tracked.extend(path for path in paths.app_css.parent.rglob("*") if path.is_file())
    for path in _unique_paths(tracked):
        if path.exists() and path.is_file():
            files.append({"path": _rel(root, path), "sha256": _sha256(path), "bytes": path.stat().st_size})
    return {
        "schema_version": manifest["schema_version"],
        "locked_at": manifest["generated_at"],
        "resources": [
            {
                "type": resource.get("type"),
                "id": resource.get("id"),
                "status": resource.get("status"),
                "reason": resource.get("reason"),
                "files": resource.get("files", []),
            }
            for resource in manifest["resources"]
        ],
        "files": files,
    }


def _build_memory_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Project Design Assets",
        "",
        f"- Generated: {manifest['generated_at']}",
        f"- App CSS: `{manifest['paths']['app_css']}`",
        f"- Manifest: `{manifest['paths']['manifest']}`",
        f"- Font CSS: `{manifest['paths']['google_fonts_css']}`",
        f"- Material Symbols CSS: `{manifest['paths']['material_symbols_css']}`",
        f"- SVG icons: `{manifest['paths']['material_design_icons']}`",
        "",
        "## Resources",
        "",
    ]
    for resource in manifest["resources"]:
        reason = resource.get("reason", "")
        lines.append(f"- `{resource.get('id')}` ({resource.get('type')}): {resource.get('status')}" + (f" - {reason}" if reason else ""))
    lines.extend(
        [
            "",
            "## Usage",
            "",
            f"Import `{manifest['paths']['app_css']}` from the project frontend entry point.",
            "The generated CSS references local `/vizo-design/...` public assets only.",
            "",
        ]
    )
    return "\n".join(lines)


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("id", "system_id", "label", "name", "title", "style", "design_system", "designSystem"):
        if key in candidate:
            summary[key] = candidate[key]
    if isinstance(candidate.get("ui_tokens"), dict):
        summary["ui_tokens"] = {
            key: value
            for key, value in candidate["ui_tokens"].items()
            if key in {"font_family", "fontFamily", "font_sans", "fontSans", "heading_font", "headingFont"}
        }
    return summary


def _npm_pack(package: str, destination: Path) -> Path:
    npm = shutil.which("npm")
    if not npm:
        raise AssetImportError("npm is not available; retry when npm/network access is available.")
    result = subprocess.run(
        [npm, "pack", package, "--silent", "--pack-destination", str(destination)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AssetImportError(f"npm pack failed for {package}: {detail or f'exit code {result.returncode}'}")
    archive_name = result.stdout.splitlines()[-1].strip() if result.stdout.splitlines() else ""
    archive = destination / archive_name
    if not archive.exists():
        raise AssetImportError(f"npm pack did not produce an archive for {package}.")
    return archive


def _extract_text_from_tar(tar: tarfile.TarFile, member_name: str) -> str:
    _safe_member_path(member_name)
    member = tar.getmember(member_name)
    file_obj = tar.extractfile(member)
    if file_obj is None:
        raise FileNotFoundError(member_name)
    return file_obj.read().decode("utf-8")


def _extract_member_from_tar(
    tar: tarfile.TarFile,
    member_name: str,
    target: Path,
    replace_existing: bool,
) -> None:
    _safe_member_path(member_name)
    if target.exists() and not replace_existing:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    member = tar.getmember(member_name)
    file_obj = tar.extractfile(member)
    if file_obj is None:
        raise FileNotFoundError(member_name)
    target.write_bytes(file_obj.read())


def _safe_posix_join(parent: PurePosixPath, source: str) -> str:
    relative = PurePosixPath(source)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe CSS asset path: {source}")
    return str(parent / relative)


def _safe_member_path(member_name: str) -> None:
    path = PurePosixPath(member_name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive path: {member_name}")


def _write_text(path: Path, content: str, replace_existing: bool) -> None:
    if path.exists() and not replace_existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _generated_header(kind: str, generated_at: str) -> str:
    return f"/* Generated by lib/design_asset_importer.py for {kind} at {generated_at}. */\n"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _styles_from_entry(entry: dict[str, Any], default: list[str]) -> list[str]:
    styles = _as_list(entry.get("styles") or entry.get("style"))
    return [str(style).strip() for style in styles if str(style).strip()] or default


def _icons_from_entry(entry: dict[str, Any]) -> list[str]:
    icons = _as_list(entry.get("icons") or entry.get("names"))
    return [str(icon).strip().replace("-", "_") for icon in icons if str(icon).strip()] or DEFAULT_ICON_NAMES


def _is_disabled(entry: dict[str, Any]) -> bool:
    return bool(entry.get("disabled") or entry.get("skip") or entry.get("enabled") is False)


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _normalize_icon_style(style: str) -> str:
    style = str(style).strip().lower()
    return {"fill": "filled", "outline": "outlined", "round": "round", "rounded": "round"}.get(style, style)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _unique_paths(values: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _reason(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__
