from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


MAX_FONT_SIZE_PX = 20.0
MAX_FONT_SIZE_REM = 1.25
MAX_RADIUS_PX = 16.0

DEFAULT_TARGETS = (
    "lib/web_console.py",
    "lib/confirm_server.py",
    "lib/chrome_bridge.py",
    "lib/templates/dialogue_console/pc.html",
    "lib/templates/dialogue_console/workbench.html",
)

_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([0-9]*\.?[0-9]+)(px|rem)")
_RADIUS_RE = re.compile(r"border-radius\s*:\s*([0-9]*\.?[0-9]+)px")
_ICON_FONT_ALLOWED_RE = re.compile(r"(\.icon\b|\.check\b|icon\{\{|check\{\{)", re.IGNORECASE)
_PILL_RADIUS_ALLOWED_RE = re.compile(r"(pill|badge|chip|status|dot|artifact|tag)", re.IGNORECASE)


@dataclass(frozen=True)
class StyleViolation:
    path: str
    line_no: int
    rule: str
    value: str
    line: str

    def format(self) -> str:
        return f"{self.path}:{self.line_no}: {self.rule}={self.value} :: {self.line.strip()}"


def _scan_font_sizes(path: Path, lines: list[str]) -> list[StyleViolation]:
    violations: list[StyleViolation] = []
    for idx, line in enumerate(lines, 1):
        if "vizo-ui-guard: ignore" in line:
            continue
        if _ICON_FONT_ALLOWED_RE.search(line):
            continue
        for match in _FONT_SIZE_RE.finditer(line):
            value = float(match.group(1))
            unit = match.group(2)
            if unit == "px" and value > MAX_FONT_SIZE_PX:
                violations.append(
                    StyleViolation(str(path), idx, "font-size", f"{value:g}{unit}", line)
                )
            if unit == "rem" and value > MAX_FONT_SIZE_REM:
                violations.append(
                    StyleViolation(str(path), idx, "font-size", f"{value:g}{unit}", line)
                )
    return violations


def _scan_radius(path: Path, lines: list[str]) -> list[StyleViolation]:
    violations: list[StyleViolation] = []
    for idx, line in enumerate(lines, 1):
        if "vizo-ui-guard: ignore" in line or "999px" in line:
            continue
        for match in _RADIUS_RE.finditer(line):
            value = float(match.group(1))
            if value > MAX_RADIUS_PX:
                violations.append(
                    StyleViolation(str(path), idx, "border-radius", f"{value:g}px", line)
                )
    return violations


def _scan_pill_radius_usage(path: Path, lines: list[str]) -> list[StyleViolation]:
    if path.suffix == ".py":
        return []
    violations: list[StyleViolation] = []
    current_selector = ""
    for idx, line in enumerate(lines, 1):
        if "vizo-ui-guard: ignore" in line or "999px" not in line:
            if "{" in line:
                current_selector = line.split("{", 1)[0]
            if "}" in line:
                current_selector = ""
            continue
        if "{" in line:
            current_selector = line.split("{", 1)[0]
        selector_context = f"{current_selector} {line}"
        if _PILL_RADIUS_ALLOWED_RE.search(selector_context):
            continue
        violations.append(
            StyleViolation(str(path), idx, "border-radius-999px", "999px", line)
        )
        if "}" in line:
            current_selector = ""
    return violations


def scan_targets(project_root: Path, targets: tuple[str, ...] = DEFAULT_TARGETS) -> list[StyleViolation]:
    violations: list[StyleViolation] = []
    for relative_path in targets:
        path = project_root / relative_path
        lines = path.read_text(encoding="utf-8").splitlines()
        violations.extend(_scan_font_sizes(path, lines))
        violations.extend(_scan_radius(path, lines))
        violations.extend(_scan_pill_radius_usage(path, lines))
    return violations
