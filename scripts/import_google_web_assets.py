#!/usr/bin/env python3
"""Import Google font and icon assets into Vizo's local static trees.

The script avoids runtime CDN dependencies by downloading npm-distributed
copies of Google-related assets.

Runtime assets are written to lib/static and referenced through /vizo/static.
Design preview assets are written to .serena/memories/design/html/assets and
referenced through relative paths so the HTML previews can be opened directly.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "lib" / "static"
FONT_DIR = STATIC_DIR / "fonts" / "google"
VENDOR_DIR = STATIC_DIR / "vendor"
ICON_DIR = STATIC_DIR / "icons" / "material-design"
DESIGN_ASSET_DIR = ROOT / ".serena" / "memories" / "design" / "html" / "assets"
DESIGN_FONT_DIR = DESIGN_ASSET_DIR / "fonts" / "google"
DESIGN_VENDOR_DIR = DESIGN_ASSET_DIR / "vendor"
DESIGN_ICON_DIR = DESIGN_ASSET_DIR / "icons" / "material-design"

FONT_PACKAGES = {
    "inter": "@fontsource/inter",
    "jetbrains-mono": "@fontsource/jetbrains-mono",
    "noto-sans-sc": "@fontsource/noto-sans-sc",
}
ICON_PACKAGES = {
    "material-symbols": "material-symbols",
    "material-design-icons-svg": "@material-design-icons/svg",
}

FONT_CSS_FILES = {
    "inter": ["latin-400.css", "latin-500.css", "latin-600.css", "latin-700.css", "latin-800.css"],
    "jetbrains-mono": ["latin-400.css", "latin-500.css", "latin-600.css"],
    "noto-sans-sc": [
        "chinese-simplified-400.css",
        "chinese-simplified-500.css",
        "chinese-simplified-600.css",
        "chinese-simplified-700.css",
    ],
}
MATERIAL_SYMBOL_STYLES = ["outlined", "rounded", "sharp"]
LEGACY_DESIGN_FONT_FILES = [
    "InterVariable.ttf",
    "JetBrainsMono-Regular.woff2",
    "JetBrainsMono-Medium.woff2",
    "JetBrainsMono-SemiBold.woff2",
    "JetBrainsMono-Bold.woff2",
    "MaterialIcons-Regular.woff2",
]


def run(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def npm_pack(package: str, destination: Path) -> Path:
    output = run(["npm", "pack", package, "--silent", "--pack-destination", str(destination)])
    archive_name = output.splitlines()[-1].strip()
    return destination / archive_name


def safe_member_path(member_name: str) -> Path:
    path = Path(member_name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive path: {member_name}")
    return path


def extract_text(archive: Path, member_name: str) -> str:
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember(member_name)
        file_obj = tar.extractfile(member)
        if file_obj is None:
            raise FileNotFoundError(member_name)
        return file_obj.read().decode("utf-8")


def write_bytes_if_changed(target: Path, content: bytes) -> bool:
    if target.exists() and target.read_bytes() == content:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return True


def write_text_if_changed(target: Path, content: str) -> bool:
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return True


def copy_file_if_changed(source: Path, target: Path) -> bool:
    return write_bytes_if_changed(target, source.read_bytes())


def extract_member(archive: Path, member_name: str, target: Path) -> None:
    safe_member_path(member_name)
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember(member_name)
        file_obj = tar.extractfile(member)
        if file_obj is None:
            raise FileNotFoundError(member_name)
        write_bytes_if_changed(target, file_obj.read())


def rewrite_fontsource_css(css: str, archive: Path, package_key: str) -> str:
    def replace(match: re.Match[str]) -> str:
        source = match.group(1).strip("\"'")
        member_name = f"package/{source.removeprefix('./')}"
        file_name = Path(source).name
        local_name = f"{package_key}-{file_name}"
        extract_member(archive, member_name, FONT_DIR / local_name)
        return f"url('/vizo/static/fonts/google/{local_name}')"

    return re.sub(r"url\(([^)]+)\)", replace, css)


def split_font_css_sections(css: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    marker_pattern = re.compile(r"^/\* (@fontsource/[^*]+) \*/\s*$", re.MULTILINE)
    matches = list(marker_pattern.finditer(css))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(css)
        sections[match.group(1)] = css[match.start() : end].strip()
    return sections


def import_fontsource_packages(tmp_dir: Path, package_keys: list[str] | None = None) -> str:
    selected_keys = package_keys or list(FONT_PACKAGES)
    existing_sections: dict[str, str] = {}
    existing_css = VENDOR_DIR / "google-fonts.css"
    if package_keys and existing_css.exists():
        existing_sections = split_font_css_sections(existing_css.read_text(encoding="utf-8"))
    generated_sections: dict[str, str] = {}

    for package_key in selected_keys:
        package_name = FONT_PACKAGES[package_key]
        archive = npm_pack(package_name, tmp_dir)
        css_blocks: list[str] = []
        for css_file in FONT_CSS_FILES[package_key]:
            css = extract_text(archive, f"package/{css_file}")
            css_blocks.append(rewrite_fontsource_css(css, archive, package_key).strip())
        generated_sections[package_name] = f"/* {package_name} */\n\n" + "\n\n".join(css_blocks)

    blocks: list[str] = ["/* Generated by scripts/import_google_web_assets.py. Do not edit by hand. */"]
    for package_key, package_name in FONT_PACKAGES.items():
        section = generated_sections.get(package_name) or existing_sections.get(package_name)
        if section:
            blocks.append(section)
    return "\n\n".join(blocks) + "\n"


def import_material_symbols(tmp_dir: Path) -> str:
    archive = npm_pack(ICON_PACKAGES["material-symbols"], tmp_dir)
    blocks: list[str] = ["/* Generated by scripts/import_google_web_assets.py. Do not edit by hand. */"]
    for style in MATERIAL_SYMBOL_STYLES:
        font_name = f"material-symbols-{style}.woff2"
        extract_member(archive, f"package/{font_name}", FONT_DIR / font_name)
        css = extract_text(archive, f"package/{style}.css")
        css = css.replace(f'url("./{font_name}")', f"url('/vizo/static/fonts/google/{font_name}')")
        css = css.replace(f"url(./{font_name})", f"url('/vizo/static/fonts/google/{font_name}')")
        blocks.append(css.strip())
    return "\n\n".join(blocks) + "\n"


def import_material_design_svgs(tmp_dir: Path) -> int:
    archive = npm_pack(ICON_PACKAGES["material-design-icons-svg"], tmp_dir)
    if ICON_DIR.exists():
        shutil.rmtree(ICON_DIR)
    ICON_DIR.mkdir(parents=True, exist_ok=True)

    count = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".svg"):
                continue
            safe_member_path(member.name)
            relative = Path(member.name).relative_to("package")
            target = ICON_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            file_obj = tar.extractfile(member)
            if file_obj is None:
                continue
            write_bytes_if_changed(target, file_obj.read())
            count += 1
    return count


def sync_design_assets(
    *,
    include_svg_icons: bool,
    font_file_prefixes: tuple[str, ...] | None = None,
    prune_legacy_fonts: bool = True,
) -> None:
    if prune_legacy_fonts:
        for file_name in LEGACY_DESIGN_FONT_FILES:
            legacy_file = DESIGN_ASSET_DIR / "fonts" / file_name
            if legacy_file.exists():
                legacy_file.unlink()

    DESIGN_FONT_DIR.mkdir(parents=True, exist_ok=True)
    for source in FONT_DIR.rglob("*"):
        if source.is_file():
            if font_file_prefixes and not source.name.startswith(font_file_prefixes):
                continue
            copy_file_if_changed(source, DESIGN_FONT_DIR / source.relative_to(FONT_DIR))

    DESIGN_VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    for css_name in ("google-fonts.css", "material-symbols.css"):
        source_css = VENDOR_DIR / css_name
        if not source_css.exists():
            continue
        css = source_css.read_text(encoding="utf-8")
        css = css.replace("/vizo/static/fonts/google/", "../fonts/google/")
        write_text_if_changed(DESIGN_VENDOR_DIR / css_name, css)

    if include_svg_icons and ICON_DIR.exists():
        if DESIGN_ICON_DIR.exists():
            shutil.rmtree(DESIGN_ICON_DIR)
        shutil.copytree(ICON_DIR, DESIGN_ICON_DIR)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-svg-icons", action="store_true", help="Do not import SVG icon files.")
    parser.add_argument(
        "--font-family",
        action="append",
        choices=sorted(FONT_PACKAGES),
        help=(
            "Only import the selected Fontsource family/package key. "
            "May be passed more than once; default imports all fonts and Material Symbols."
        ),
    )
    parser.add_argument(
        "--include-material-symbols",
        action="store_true",
        help="Also import Material Symbols when --font-family is used.",
    )
    parser.add_argument(
        "--skip-design-sync",
        action="store_true",
        help="Do not sync generated assets into .serena/memories/design/html/assets.",
    )
    args = parser.parse_args(argv)

    FONT_DIR.mkdir(parents=True, exist_ok=True)
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="vizo-google-assets-") as tmp:
        tmp_dir = Path(tmp)
        font_keys = args.font_family or None
        fonts_css = import_fontsource_packages(tmp_dir, font_keys)
        write_text_if_changed(VENDOR_DIR / "google-fonts.css", fonts_css)
        import_symbols = args.include_material_symbols or font_keys is None
        if import_symbols:
            symbols_css = import_material_symbols(tmp_dir)
            write_text_if_changed(VENDOR_DIR / "material-symbols.css", symbols_css)
        svg_count = 0 if args.skip_svg_icons else import_material_design_svgs(tmp_dir)

    if not args.skip_design_sync:
        selected_prefixes = tuple(f"{font_key}-" for font_key in font_keys) if font_keys else None
        sync_design_assets(
            include_svg_icons=not args.skip_svg_icons,
            font_file_prefixes=selected_prefixes,
            prune_legacy_fonts=font_keys is None,
        )

    print(f"Wrote {VENDOR_DIR / 'google-fonts.css'}")
    if args.include_material_symbols or not args.font_family:
        print(f"Wrote {VENDOR_DIR / 'material-symbols.css'}")
    print(f"Wrote Google font files to {FONT_DIR}")
    if not args.skip_svg_icons:
        print(f"Wrote {svg_count} SVG icons to {ICON_DIR}")
    if not args.skip_design_sync:
        print(f"Synced design preview assets to {DESIGN_ASSET_DIR}")


if __name__ == "__main__":
    main()
