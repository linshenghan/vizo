from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runner import AgentRunner
from lib.paths import iter_task_dirs
from lib.runtime.events import utcnow_iso


logger = logging.getLogger(__name__)

_UI_REQUEST_KEYWORDS = (
    "demo",
    "页面",
    "界面",
    "首页",
    "落地页",
    "landing",
    "dashboard",
    "控制台",
    "工作台",
    "后台",
    "web",
    "app",
    "移动端",
    "pc",
    "前端",
    "ui",
)
_UI_ACTION_KEYWORDS = (
    "生成",
    "做",
    "设计",
    "搭",
    "出",
    "给我",
    "帮我",
    "看",
    "看看",
    "预览",
    "原型",
)
_GREENFIELD_PRODUCT_KEYWORDS = (
    "微信小程序",
    "小程序",
    "miniapp",
    "mini app",
    "app",
    "应用",
    "产品",
    "网站",
    "官网",
    "落地页",
    "landing page",
    "h5",
)
_GREENFIELD_BRIEF_KEYWORDS = (
    "使用场景",
    "适用场景",
    "核心功能",
    "主要功能",
    "目标用户",
    "面向",
    "面对",
    "主要是",
    "高级功能",
)
_GREENFIELD_STRONG_SIGNALS = (
    "微信小程序",
    "小程序",
    "官网",
    "落地页",
    "landing page",
)
_CURRENT_PROJECT_CONTINUATION_HINTS = (
    "继续",
    "沿用",
    "复用",
    "按当前默认",
    "按当前风格",
    "按现有",
    "已有页面",
    "现有页面",
    "当前项目",
)
_STYLE_RESELECT_KEYWORDS = (
    "换一种风格",
    "换个风格",
    "换一套",
    "换套",
    "重新选",
    "重选",
    "重新帮我选",
    "重新推荐",
    "另一种风格",
    "另一套",
    "更大胆",
    "更极简",
    "更克制",
)
_STYLE_ADOPT_KEYWORDS = (
    "采纳",
    "设为默认",
    "项目默认",
    "作为默认",
    "后面都按这个",
    "以后都按这个",
    "长期按这个",
    "替换当前默认",
    "替换默认",
    "定为默认",
)
_STYLE_KEEP_CURRENT_KEYWORDS = (
    "继续按当前默认",
    "继续按现在的",
    "继续按原来的",
    "继续按当前风格",
)
_PREVIEW_REFERENCES = (
    "刚才这套",
    "刚刚这套",
    "当前这套",
    "这套",
    "这个方案",
)
_INDEX_ALIASES = {
    "一": 1,
    "二": 2,
    "三": 3,
    "1": 1,
    "2": 2,
    "3": 3,
    "a": 1,
    "b": 2,
    "c": 3,
    "A": 1,
    "B": 2,
    "C": 3,
}
_PREVIEW_STATE_KEY = "interaction_design_preview"
_CANDIDATE_ROUND_KEY = "interaction_design_candidates"
_SYSTEM_VERSION = 1
_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DIR = _ROOT / "agents" / "_builtin" / "interaction_design"
_MANIFEST_PATH = _BUILTIN_DIR / "manifest.json"
_ROLE_TEMPLATE_PATH = _BUILTIN_DIR / "roles" / "interaction_designer.md"
_CATALOG_PATH = _BUILTIN_DIR / "catalog" / "style_catalog.json"


@dataclass
class InteractionDesignInterception:
    handled: bool = False
    continue_to_runtime: bool = False
    assistant_text: str = ""
    design_context: str = ""
    session_changed: bool = False
    reason: str = ""


class InteractionDesignService:
    """Main-session helper for preview/adopted UI design-system flows."""

    def __init__(self, *, agent_runner: AgentRunner | None = None) -> None:
        self.agent_runner = agent_runner

    async def maybe_intercept(
        self,
        *,
        session,
        session_dir: Path | str,
        project_root: Path | str,
        content: str,
        attachments: list[str] | None = None,
    ) -> InteractionDesignInterception:
        del attachments
        text = str(content or "").strip()
        if not text:
            return InteractionDesignInterception()

        session_dir_path = Path(session_dir)
        project_root_path = Path(project_root)
        adopted = self.load_adopted_design(project_root_path)
        preview = self._get_preview_state(session)
        candidate_round = self._get_candidate_round(session)
        page_request = self.is_ui_generation_request(text)
        reselect_request = self.is_reselect_request(text)
        exploratory_request = self.is_exploratory_ui_request(text)

        if reselect_request:
            self._clear_preview_state(session)
            candidates = await self.generate_candidates(
                session=session,
                session_dir=session_dir_path,
                project_root=project_root_path,
                content=text,
                adopted=adopted,
                replace_existing=bool(adopted),
            )
            self._set_candidate_round(
                session,
                {
                    "created_at": utcnow_iso(),
                    "replace_existing": bool(adopted),
                    "source_request": text,
                    "candidates": candidates,
                },
            )
            return InteractionDesignInterception(
                handled=True,
                assistant_text=self._format_candidates_message(
                    candidates,
                    replace_existing=bool(adopted),
                ),
                session_changed=True,
                reason="reselect_candidates",
            )

        if candidate_round:
            selection = self.parse_candidate_selection(text, candidate_round.get("candidates", []))
            if selection:
                replace_existing = bool(candidate_round.get("replace_existing"))
                self._set_preview_state(
                    session,
                    {
                        "selected_at": utcnow_iso(),
                        "replace_existing": replace_existing,
                        "candidate": selection,
                    },
                )
                self._clear_candidate_round(session)
                if self.is_adopt_request(text):
                    adopted_state = self.adopt_design(
                        project_root=project_root_path,
                        candidate=selection,
                        replace_existing=replace_existing,
                    )
                    self._clear_candidate_round(session)
                    self._clear_preview_state(session)
                    if page_request:
                        return InteractionDesignInterception(
                            continue_to_runtime=True,
                            design_context=self.build_runtime_context(adopted_state, mode="adopted"),
                            session_changed=True,
                            reason="selected_and_adopted_continue",
                        )
                    return InteractionDesignInterception(
                        handled=True,
                        assistant_text=self._format_adopted_message(adopted_state, replace_existing=replace_existing),
                        session_changed=True,
                        reason="selected_and_adopted_ack",
                    )

                if page_request:
                    return InteractionDesignInterception(
                        continue_to_runtime=True,
                        design_context=self.build_runtime_context(selection, mode="preview"),
                        session_changed=True,
                        reason="selected_preview_continue",
                    )
                return InteractionDesignInterception(
                    handled=True,
                    assistant_text=self._format_preview_ready_message(selection),
                    session_changed=True,
                    reason="selected_preview_ack",
                )

            if page_request and not self._should_keep_current_default(text):
                return InteractionDesignInterception(
                    handled=True,
                    assistant_text=self._format_pending_selection_message(candidate_round.get("candidates", [])),
                    reason="candidate_round_waiting_selection",
                )

        if preview and self.is_adopt_request(text):
            preview_candidate = dict(preview.get("candidate") or {})
            replace_existing = bool(preview.get("replace_existing"))
            adopted_state = self.adopt_design(
                project_root=project_root_path,
                candidate=preview_candidate,
                replace_existing=replace_existing,
            )
            self._clear_candidate_round(session)
            self._clear_preview_state(session)
            if page_request:
                return InteractionDesignInterception(
                    continue_to_runtime=True,
                    design_context=self.build_runtime_context(adopted_state, mode="adopted"),
                    session_changed=True,
                    reason="adopt_preview_continue",
                )
            return InteractionDesignInterception(
                handled=True,
                assistant_text=self._format_adopted_message(adopted_state, replace_existing=replace_existing),
                session_changed=True,
                reason="adopt_preview_ack",
            )

        if page_request:
            if preview:
                return InteractionDesignInterception(
                    continue_to_runtime=True,
                    design_context=self.build_runtime_context(preview.get("candidate") or {}, mode="preview"),
                    reason="reuse_preview_context",
                )
            if adopted and not exploratory_request and not self._should_keep_current_default(text):
                return InteractionDesignInterception(
                    continue_to_runtime=True,
                    design_context=self.build_runtime_context(adopted, mode="adopted"),
                    reason="reuse_adopted_context",
                )
            if adopted and self._should_keep_current_default(text):
                return InteractionDesignInterception(
                    continue_to_runtime=True,
                    design_context=self.build_runtime_context(adopted, mode="adopted"),
                    reason="reuse_adopted_context",
                )
            candidates = await self.generate_candidates(
                session=session,
                session_dir=session_dir_path,
                project_root=project_root_path,
                content=text,
                adopted=adopted,
                replace_existing=bool(adopted),
            )
            self._set_candidate_round(
                session,
                {
                    "created_at": utcnow_iso(),
                    "replace_existing": bool(adopted),
                    "source_request": text,
                    "candidates": candidates,
                },
            )
            return InteractionDesignInterception(
                handled=True,
                assistant_text=self._format_candidates_message(candidates, replace_existing=bool(adopted)),
                session_changed=True,
                reason="initial_candidates",
            )

        return InteractionDesignInterception()

    async def generate_candidates(
        self,
        *,
        session,
        session_dir: Path,
        project_root: Path,
        content: str,
        adopted: dict[str, Any] | None,
        replace_existing: bool,
    ) -> list[dict[str, Any]]:
        catalog = self._load_catalog()
        agent_candidates = await self._generate_candidates_with_agent(
            session=session,
            session_dir=session_dir,
            project_root=project_root,
            content=content,
            catalog=catalog,
            adopted=adopted,
            replace_existing=replace_existing,
        )
        if agent_candidates:
            return agent_candidates[:3]
        return self._generate_candidates_with_rules(
            content=content,
            catalog=catalog,
            adopted=adopted,
            replace_existing=replace_existing,
        )

    def load_adopted_design(self, project_root: Path | str) -> dict[str, Any] | None:
        root = Path(project_root)
        state_path = self._design_system_path(root)
        if state_path.exists():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            adopted = payload.get("adopted") if isinstance(payload, dict) else None
            if isinstance(adopted, dict) and adopted:
                return adopted

        design_dir = self._design_memories_dir(root)
        files = {
            "index": design_dir / "index.md",
            "visual_system": design_dir / "visual-system.md",
            "design_spec": design_dir / "design-spec.md",
            "ui_tokens": design_dir / "ui-tokens.md",
        }
        if not any(path.exists() for path in files.values()):
            return None
        result: dict[str, Any] = {
            "system_id": "existing_project_design",
            "label": "当前项目既有设计规范",
            "summary": "项目已存在设计记忆，请严格复用已有规范。",
            "rationale": "该项目此前已沉淀设计规范，后续页面生成应保持一致性。",
            "keywords": ["existing-design"],
            "design_spec": {},
            "ui_tokens": {},
            "source": {"kind": "project_memory"},
        }
        for key, path in files.items():
            if path.exists():
                result[f"{key}_markdown"] = path.read_text(encoding="utf-8").strip()
        return result

    def adopt_design(
        self,
        *,
        project_root: Path | str,
        candidate: dict[str, Any],
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        root = Path(project_root)
        root.mkdir(parents=True, exist_ok=True)
        design_dir = self._design_memories_dir(root)
        design_dir.mkdir(parents=True, exist_ok=True)

        adopted = dict(candidate)
        adopted["adopted_at"] = utcnow_iso()
        source = dict(candidate.get("source") or {})
        source.setdefault("kind", "interaction_design")
        source.setdefault("manifest", str(_MANIFEST_PATH))
        adopted["source"] = source
        adopted["resources"] = self._normalize_resources(
            adopted.get("resources"),
            ui_tokens=adopted.get("ui_tokens") if isinstance(adopted.get("ui_tokens"), dict) else {},
            motion_direction=str(adopted.get("motion_direction") or ""),
        )
        adopted["asset_import"] = self._import_design_assets(
            project_root=root,
            candidate=adopted,
            replace_existing=replace_existing,
        )

        state_payload = {
            "version": _SYSTEM_VERSION,
            "updated_at": adopted["adopted_at"],
            "adopted": adopted,
        }
        self._design_system_path(root).parent.mkdir(parents=True, exist_ok=True)
        self._design_system_path(root).write_text(
            json.dumps(state_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        files = self._render_design_memory_files(adopted, replace_existing=replace_existing)
        for relative_name, content in files.items():
            (design_dir / relative_name).write_text(content, encoding="utf-8")
        return adopted

    def load_latest_task_design(
        self,
        project_root: Path | str,
        *,
        task_id: str = "",
    ) -> dict[str, Any] | None:
        root = Path(project_root)
        requested_task_id = str(task_id or "").strip()
        for task_dir in iter_task_dirs(root):
            if not task_dir.name.startswith("hub-"):
                continue
            state_file = task_dir / "state.json"
            if not state_file.exists():
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if requested_task_id and str(state.get("id") or "") != requested_task_id:
                continue
            if str(state.get("module_id") or "") != "interaction_design":
                continue
            if str(state.get("status") or "") != "completed":
                continue
            candidate = self._load_task_design_candidate(task_dir=task_dir, state=state)
            if candidate:
                return candidate
        return None

    @staticmethod
    def is_ui_generation_request(text: str) -> bool:
        lowered = str(text or "").lower()
        if any(keyword in lowered for keyword in _STYLE_ADOPT_KEYWORDS):
            return any(keyword in lowered for keyword in _UI_REQUEST_KEYWORDS)
        return (
            any(keyword in lowered for keyword in _UI_REQUEST_KEYWORDS)
            and any(keyword in lowered for keyword in _UI_ACTION_KEYWORDS)
        )

    @staticmethod
    def is_reselect_request(text: str) -> bool:
        lowered = str(text or "").lower()
        return any(keyword in lowered for keyword in _STYLE_RESELECT_KEYWORDS)

    @staticmethod
    def is_exploratory_ui_request(text: str) -> bool:
        lowered = str(text or "").lower()
        if not lowered:
            return False
        if any(keyword in lowered for keyword in _CURRENT_PROJECT_CONTINUATION_HINTS):
            return False
        if any(keyword in lowered for keyword in _GREENFIELD_STRONG_SIGNALS):
            return True
        return (
            any(keyword in lowered for keyword in _GREENFIELD_PRODUCT_KEYWORDS)
            and any(keyword in lowered for keyword in _GREENFIELD_BRIEF_KEYWORDS)
        )

    @staticmethod
    def is_adopt_request(text: str) -> bool:
        lowered = str(text or "").lower()
        return any(keyword in lowered for keyword in _STYLE_ADOPT_KEYWORDS)

    @staticmethod
    def parse_candidate_selection(text: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        raw = str(text or "")
        lowered = raw.lower()
        for candidate in candidates:
            for alias in InteractionDesignService._candidate_aliases(candidate):
                if alias and alias.lower() in lowered:
                    return dict(candidate)

        patterns = (
            r"(?:方案|候选|选|第)\s*([ABCabc123一二三])",
            r"([ABCabc])\s*方案",
            r"第\s*([一二三123])\s*套",
        )
        for pattern in patterns:
            match = re.search(pattern, raw)
            if not match:
                continue
            idx = _INDEX_ALIASES.get(match.group(1))
            if not idx:
                continue
            if 1 <= idx <= len(candidates):
                return dict(candidates[idx - 1])
        return None

    @staticmethod
    def build_runtime_context(design: dict[str, Any], *, mode: str) -> str:
        label = str(design.get("label") or design.get("name") or design.get("system_id") or "未命名方案")
        summary = str(design.get("summary") or "")
        rationale = str(design.get("rationale") or "")
        ui_tokens = design.get("ui_tokens") if isinstance(design.get("ui_tokens"), dict) else {}
        design_spec = design.get("design_spec") if isinstance(design.get("design_spec"), dict) else {}
        lines = [
            f"当前模式：{'项目已采纳设计系统' if mode == 'adopted' else '当前会话预览设计系统'}",
            f"当前视觉系统：{label}",
        ]
        if summary:
            lines.append(f"视觉摘要：{summary}")
        if rationale:
            lines.append(f"选择原因：{rationale}")
        if design.get("color_direction"):
            lines.append(f"色彩方向：{design['color_direction']}")
        if design.get("typography_direction"):
            lines.append(f"字体方向：{design['typography_direction']}")
        if design.get("component_direction"):
            lines.append(f"组件气质：{design['component_direction']}")
        if design.get("motion_direction"):
            lines.append(f"动效方向：{design['motion_direction']}")
        if design.get("keywords"):
            lines.append("风格关键词：" + " / ".join(str(item) for item in design.get("keywords") or []))

        constraint_lines = []
        for key in (
            "density",
            "layout",
            "type_scale",
            "radius_policy",
            "component_rules",
            "interaction_rules",
        ):
            value = str(design_spec.get(key) or "").strip()
            if value:
                constraint_lines.append(f"- {value}")
        if constraint_lines:
            lines.append("必须遵守的设计约束：")
            lines.extend(constraint_lines)
        elif design.get("design_spec_markdown"):
            lines.append("项目已有设计规范：")
            lines.append(str(design.get("design_spec_markdown")).strip())

        token_lines = []
        for key in (
            "font_size_base",
            "font_size_title",
            "radius_sm",
            "radius_md",
            "radius_lg",
            "shadow",
            "primary",
            "surface",
            "border",
            "font_family",
        ):
            value = str(ui_tokens.get(key) or "").strip()
            if value:
                token_lines.append(f"- {key}: {value}")
        if token_lines:
            lines.append("优先复用以下 token，避免擅自做大字号和大圆角：")
            lines.extend(token_lines)
        elif design.get("ui_tokens_markdown"):
            lines.append("项目已有 token 规范：")
            lines.append(str(design.get("ui_tokens_markdown")).strip())

        asset_import = design.get("asset_import") if isinstance(design.get("asset_import"), dict) else {}
        if mode == "adopted" and asset_import:
            css_paths = InteractionDesignService._collect_asset_css_paths(asset_import)
            lines.append("资源引用约束：")
            if css_paths:
                lines.append(
                    "- 优先 import 本地资源 CSS："
                    + " / ".join(css_paths)
                    + "；不要改用 CDN 字体或图标资源。"
                )
            else:
                lines.append(
                    "- 优先 import 本地 `src/styles/vizo-design.css`；如果资源 manifest 提供 CSS 路径，以 manifest 为准。"
                )
            lines.append("- 采用本地资源声明中的字体、图标和 CSS motion，不要用远程 CDN 绕过项目资源库。")
            if not InteractionDesignService._asset_import_succeeded(asset_import):
                lines.append(
                    f"- 当前资源导入状态：{asset_import.get('status', 'pending')}；"
                    "缺失资源按设计记忆中的资源声明补齐后再使用。"
                )

        lines.extend(
            [
                "输出要求：",
                "- 页面、组件和文案层级都必须服从以上设计系统。",
                "- 不要自行放大字号、圆角、阴影或间距来制造“设计感”。",
                "- 如需新组件，也要沿用当前视觉系统的密度、圆角、字重和表面层级。",
            ]
        )
        return "\n".join(lines).strip()

    async def _generate_candidates_with_agent(
        self,
        *,
        session,
        session_dir: Path,
        project_root: Path,
        content: str,
        catalog: list[dict[str, Any]],
        adopted: dict[str, Any] | None,
        replace_existing: bool,
    ) -> list[dict[str, Any]]:
        if not self.agent_runner:
            return []
        if not bool((self.agent_runner.config or {}).get("interaction_design_use_agent_runner", False)):
            return []
        prompt = self._build_agent_prompt(
            session=session,
            project_root=project_root,
            content=content,
            catalog=catalog,
            adopted=adopted,
            replace_existing=replace_existing,
        )
        agent_task_dir = session_dir / "interaction-design"
        agent_task_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await self.agent_runner.run(
                role="interaction_designer",
                task_dir=str(agent_task_dir),
                input_docs={},
                cwd=str(project_root),
                project=str((session.metadata or {}).get("project_name") or project_root.name),
                prompt_override=prompt,
            )
        except Exception as error:
            logger.info("interaction designer agent fallback to rules: %s", error)
            return []
        data = result.data if hasattr(result, "data") else {}
        if not isinstance(data, dict):
            return []
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            return []
        normalized = [self._normalize_candidate(item, index=i + 1) for i, item in enumerate(candidates)]
        return [item for item in normalized if item]

    def _build_agent_prompt(
        self,
        *,
        session,
        project_root: Path,
        content: str,
        catalog: list[dict[str, Any]],
        adopted: dict[str, Any] | None,
        replace_existing: bool,
    ) -> str:
        role_template = _ROLE_TEMPLATE_PATH.read_text(encoding="utf-8").strip() if _ROLE_TEMPLATE_PATH.exists() else ""
        project_name = str((session.metadata or {}).get("project_name") or project_root.name)
        existing = ""
        if adopted:
            existing = (
                "\n## 当前已采纳设计系统\n"
                f"- 名称：{adopted.get('label', '')}\n"
                f"- 摘要：{adopted.get('summary', '')}\n"
                f"- 替换场景：{'是' if replace_existing else '否'}\n"
            )
        return (
            f"{role_template}\n\n"
            "## 任务\n"
            "你要为当前项目从给定 style catalog 中挑选 2 到 3 套候选视觉系统。\n"
            "请直接输出 JSON，不要输出解释性前后文。\n\n"
            f"## 当前项目\n- 项目：{project_name}\n- 路径：{project_root}\n"
            f"{existing}\n"
            "## 用户原始需求\n"
            f"{content}\n\n"
            "## Style Catalog\n"
            f"{json.dumps(catalog, ensure_ascii=False, indent=2)}\n\n"
            "## 输出 JSON Schema\n"
            "{\n"
            '  "status": "success",\n'
            '  "summary": "一句中文总结",\n'
            '  "candidates": [\n'
            "    {\n"
            '      "system_id": "catalog id",\n'
            '      "label": "中文风格名",\n'
            '      "summary": "一句摘要",\n'
            '      "rationale": "为什么适合",\n'
            '      "color_direction": "色彩方向",\n'
            '      "typography_direction": "字体方向",\n'
            '      "component_direction": "组件气质",\n'
            '      "motion_direction": "动效方向",\n'
            '      "keywords": ["关键词1", "关键词2"],\n'
            '      "design_spec": {\n'
            '        "density": "密度规则",\n'
            '        "layout": "布局规则",\n'
            '        "type_scale": "字号规则",\n'
            '        "radius_policy": "圆角规则",\n'
            '        "component_rules": "组件规则",\n'
            '        "interaction_rules": "交互规则"\n'
            "      },\n"
            '      "ui_tokens": {\n'
            '        "font_family": "字体",\n'
            '        "font_size_base": "14px",\n'
            '        "font_size_title": "28px",\n'
            '        "radius_sm": "8px",\n'
            '        "radius_md": "12px",\n'
            '        "radius_lg": "16px",\n'
            '        "primary": "#xxxxxx",\n'
            '        "surface": "#xxxxxx",\n'
            '        "border": "#xxxxxx",\n'
            '        "shadow": "css shadow"\n'
            "      },\n"
            '      "resources": {\n'
            '        "fonts": [\n'
            '          {"family": "字体家族名", "source": "local-or-google-fonts", "usage": "primary-ui"}\n'
            "        ],\n"
            '        "icons": [\n'
            '          {"name": "Material Symbols Rounded", "source": "material-symbols", "usage": "ui-icons"}\n'
            "        ],\n"
            '        "motion": {\n'
            '          "type": "css",\n'
            '          "duration": "120ms-220ms",\n'
            '          "easing": "cubic-bezier(0.2, 0, 0, 1)",\n'
            '          "notes": "资源声明，只描述后续应导入的 CSS motion token，不声称已经导入。"\n'
            "        }\n"
            "      }\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "注意：`resources` 只是资源声明，不能声称字体、图标或 CSS 已经导入项目；真正导入只会在用户明确采纳后由 adopt 链路完成。\n"
        ).strip()

    def _generate_candidates_with_rules(
        self,
        *,
        content: str,
        catalog: list[dict[str, Any]],
        adopted: dict[str, Any] | None,
        replace_existing: bool,
    ) -> list[dict[str, Any]]:
        lowered = content.lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in catalog:
            tags = [str(tag).lower() for tag in item.get("fit_tags", [])]
            score = 0
            for tag in tags:
                if tag and tag in lowered:
                    score += 3
            for keyword in item.get("keywords", []):
                if str(keyword).lower() in lowered:
                    score += 2
            if any(term in lowered for term in ("dashboard", "控制台", "后台", "监控", "企业", "数据")) and "console" in tags:
                score += 2
            if any(term in lowered for term in ("consumer", "用户", "app", "增长", "社区", "内容")) and "consumer" in tags:
                score += 2
            if any(term in lowered for term in ("ai", "智能", "创新", "bold", "大胆")) and "bold" in tags:
                score += 2
            if replace_existing and adopted and item.get("system_id") == adopted.get("system_id"):
                score -= 5
            scored.append((score, item))

        scored.sort(key=lambda entry: (entry[0], entry[1].get("priority", 0)), reverse=True)
        top = [self._normalize_candidate(item, index=i + 1) for i, (_, item) in enumerate(scored[:3])]
        return [item for item in top if item]

    @staticmethod
    def _normalize_candidate(item: dict[str, Any], *, index: int) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        candidate = dict(item)
        candidate["id"] = candidate.get("id") or chr(ord("A") + index - 1)
        candidate["label"] = str(candidate.get("label") or candidate.get("name") or candidate.get("system_id") or f"方案{index}")
        candidate["summary"] = str(candidate.get("summary") or "")
        candidate["rationale"] = str(candidate.get("rationale") or "")
        candidate["keywords"] = [str(keyword) for keyword in candidate.get("keywords", [])]
        candidate["design_spec"] = dict(candidate.get("design_spec") or {})
        candidate["ui_tokens"] = dict(candidate.get("ui_tokens") or {})
        candidate["resources"] = InteractionDesignService._normalize_resources(
            candidate.get("resources"),
            ui_tokens=candidate["ui_tokens"],
            motion_direction=str(candidate.get("motion_direction") or ""),
        )
        return candidate

    @staticmethod
    def _candidate_aliases(candidate: dict[str, Any]) -> list[str]:
        aliases = [
            str(candidate.get("id") or ""),
            f"方案{candidate.get('id', '')}",
            f"候选{candidate.get('id', '')}",
            str(candidate.get("system_id") or ""),
            str(candidate.get("label") or ""),
        ]
        if str(candidate.get("id") or "").upper() in {"A", "B", "C"}:
            idx = ord(str(candidate["id"]).upper()) - ord("A") + 1
            aliases.extend([f"方案{idx}", f"第{idx}套"])
        return [alias for alias in aliases if alias]

    @staticmethod
    def _format_candidates_message(candidates: list[dict[str, Any]], *, replace_existing: bool) -> str:
        headline = "我先给你 3 套候选视觉系统。" if len(candidates) >= 3 else "我先给你几套候选视觉系统。"
        if replace_existing:
            headline = "我先给你一轮新的候选视觉系统；在你明确确认替换前，项目仍沿用当前默认设计系统。"
        lines = [
            headline,
            "你可以直接回复：",
            "- “先按方案A做一个 demo”",
            "- “把方案B设为项目默认，再做首页”",
            "- “重新给我一套更大胆的”",
            "",
        ]
        for candidate in candidates:
            lines.extend(
                [
                    f"## 方案 {candidate.get('id', '')} | {candidate.get('label', '')}",
                    f"- 视觉摘要：{candidate.get('summary', '')}",
                    f"- 适配原因：{candidate.get('rationale', '')}",
                    f"- 色彩方向：{candidate.get('color_direction', '')}",
                    f"- 字体方向：{candidate.get('typography_direction', '')}",
                    f"- 组件气质：{candidate.get('component_direction', '')}",
                    f"- 动效方向：{candidate.get('motion_direction', '')}",
                    f"- 关键词：{' / '.join(candidate.get('keywords') or [])}",
                    "",
                ]
            )
        return "\n".join(lines).strip()

    @staticmethod
    def _format_pending_selection_message(candidates: list[dict[str, Any]]) -> str:
        if not candidates:
            return "这轮视觉系统候选还没准备好，请重新告诉我你想做什么页面。"
        labels = " / ".join(f"{item.get('id', '')}:{item.get('label', '')}" for item in candidates)
        return (
            "你这轮还没有明确选定视觉系统，暂时不会直接生成页面。\n"
            f"当前可选方案：{labels}\n"
            "请直接回复类似：\n"
            "- 先按方案A做首页\n"
            "- 把方案B设为项目默认，再做 dashboard"
        )

    @staticmethod
    def _format_preview_ready_message(candidate: dict[str, Any]) -> str:
        return (
            f"已切到方案 {candidate.get('id', '')}《{candidate.get('label', '')}》的预览态。\n"
            "这还不会写入项目长期设计记忆。\n"
            "你现在可以继续说：\n"
            f"- 先按方案{candidate.get('id', '')}做一个 demo\n"
            f"- 把方案{candidate.get('id', '')}设为项目默认"
        )

    @staticmethod
    def _format_adopted_message(candidate: dict[str, Any], *, replace_existing: bool) -> str:
        action = "已替换项目默认视觉系统" if replace_existing else "已设为项目默认视觉系统"
        lines = [
            f"{action}：方案 {candidate.get('id', '')}《{candidate.get('label', '')}》。",
            "后续主会话和前端相关角色都会默认复用这套设计规范。",
        ]
        asset_import = candidate.get("asset_import") if isinstance(candidate.get("asset_import"), dict) else {}
        if asset_import and InteractionDesignService._asset_import_succeeded(asset_import):
            css_paths = InteractionDesignService._collect_asset_css_paths(asset_import)
            if css_paths:
                lines.append("本地设计资源已导入，前端请优先引用：" + " / ".join(css_paths))
            else:
                lines.append("本地设计资源已导入，前端请优先引用资源 manifest 或 `src/styles/vizo-design.css`。")
        else:
            resources = candidate.get("resources") if isinstance(candidate.get("resources"), dict) else {}
            pending = InteractionDesignService._format_unready_asset_resources(asset_import, fallback_resources=resources)
            status = str(asset_import.get("status") or "pending")
            error = str(asset_import.get("error") or asset_import.get("message") or "").strip()
            lines.append(f"资源导入状态：{status}；待处理资源：{pending or '字体、图标和 CSS motion 声明'}。")
            if error:
                lines.append(f"导入错误：{error}")
        return "\n".join(lines).strip()

    @staticmethod
    def _normalize_resources(
        resources: Any,
        *,
        ui_tokens: dict[str, Any],
        motion_direction: str = "",
    ) -> dict[str, Any]:
        raw = resources if isinstance(resources, dict) else {}
        defaults = InteractionDesignService._default_resources_from_tokens(
            ui_tokens=ui_tokens,
            motion_direction=motion_direction,
        )
        normalized: dict[str, Any] = {}

        fonts = raw.get("fonts")
        normalized_fonts: list[dict[str, Any]] = []
        if isinstance(fonts, list):
            for item in fonts:
                if isinstance(item, dict):
                    family = str(item.get("family") or item.get("name") or "").strip()
                    if not family:
                        continue
                    entry = dict(item)
                    entry["family"] = family
                    entry.setdefault("source", "declared")
                    entry.setdefault("usage", "primary-ui")
                    normalized_fonts.append(entry)
                elif str(item).strip():
                    normalized_fonts.append(
                        {"family": str(item).strip(), "source": "declared", "usage": "primary-ui"}
                    )
        normalized["fonts"] = normalized_fonts or defaults["fonts"]

        icons = raw.get("icons")
        normalized_icons: list[dict[str, Any]] = []
        if isinstance(icons, list):
            for item in icons:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("family") or "").strip()
                    if not name:
                        continue
                    entry = dict(item)
                    entry["name"] = name
                    entry.setdefault("source", "declared")
                    entry.setdefault("usage", "ui-icons")
                    normalized_icons.append(entry)
                elif str(item).strip():
                    normalized_icons.append(
                        {"name": str(item).strip(), "source": "declared", "usage": "ui-icons"}
                    )
        normalized["icons"] = normalized_icons or defaults["icons"]

        motion = raw.get("motion")
        normalized_motion = dict(motion) if isinstance(motion, dict) else {}
        merged_motion = dict(defaults["motion"])
        merged_motion.update({key: value for key, value in normalized_motion.items() if value not in (None, "")})
        normalized["motion"] = merged_motion
        return normalized

    @staticmethod
    def _default_resources_from_tokens(*, ui_tokens: dict[str, Any], motion_direction: str = "") -> dict[str, Any]:
        font_family = str(ui_tokens.get("font_family") or "").strip()
        font_names = InteractionDesignService._parse_font_families(font_family)
        primary_font = font_names[0] if font_names else "Inter"
        fallbacks = [font for font in font_names[1:] if font]
        return {
            "fonts": [
                {
                    "family": primary_font,
                    "fallbacks": fallbacks or ["PingFang SC", "sans-serif"],
                    "source": "google_fonts",
                    "usage": "primary-ui",
                }
            ],
            "icons": [
                {
                    "name": "Material Symbols Rounded",
                    "source": "material_symbols",
                    "usage": "ui-icons",
                    "style": "rounded",
                }
            ],
            "motion": {
                "type": "css",
                "source": "css",
                "duration": "120ms-220ms",
                "easing": "cubic-bezier(0.2, 0, 0, 1)",
                "notes": motion_direction or "Lightweight CSS transition tokens for state changes.",
            },
        }

    @staticmethod
    def _parse_font_families(font_family: str) -> list[str]:
        result: list[str] = []
        for part in str(font_family or "").split(","):
            name = part.strip().strip("\"'")
            if name:
                result.append(name)
        return result

    @staticmethod
    def _import_design_assets(
        *,
        project_root: Path,
        candidate: dict[str, Any],
        replace_existing: bool,
    ) -> dict[str, Any]:
        try:
            from lib.design_asset_importer import import_design_assets_for_project
        except Exception as error:
            return {
                "status": "pending",
                "success": False,
                "error": f"{type(error).__name__}: {error}",
                "reason": "design_asset_importer_unavailable",
            }

        try:
            result = import_design_assets_for_project(
                project_root,
                candidate,
                replace_existing=replace_existing,
            )
        except Exception as error:
            return {
                "status": "failed",
                "success": False,
                "error": f"{type(error).__name__}: {error}",
                "reason": "design_asset_import_failed",
            }

        if isinstance(result, dict):
            payload = dict(result)
            payload.setdefault("status", InteractionDesignService._infer_asset_import_status(payload))
            payload.setdefault("success", InteractionDesignService._asset_import_succeeded(payload))
            return payload
        return {
            "status": "imported",
            "success": True,
            "result": str(result) if result is not None else "",
        }

    @staticmethod
    def _asset_import_succeeded(asset_import: dict[str, Any]) -> bool:
        summary = asset_import.get("summary") if isinstance(asset_import.get("summary"), dict) else {}
        if summary:
            failed = int(summary.get("failed", 0) or 0)
            pending = int(summary.get("pending", 0) or 0)
            imported = int(summary.get("imported", 0) or 0)
            return failed == 0 and pending == 0 and imported > 0
        status = str(asset_import.get("status") or "").lower()
        if asset_import.get("success") is True:
            return status not in {"failed", "error", "pending", "skipped"}
        return status in {"success", "succeeded", "imported", "ready", "ok"}

    @staticmethod
    def _infer_asset_import_status(asset_import: dict[str, Any]) -> str:
        summary = asset_import.get("summary") if isinstance(asset_import.get("summary"), dict) else {}
        if not summary:
            return "imported"
        failed = int(summary.get("failed", 0) or 0)
        pending = int(summary.get("pending", 0) or 0)
        imported = int(summary.get("imported", 0) or 0)
        if failed == 0 and pending == 0 and imported > 0:
            return "imported"
        if imported > 0:
            return "partial"
        if failed > 0:
            return "failed"
        if pending > 0:
            return "pending"
        return "skipped"

    @staticmethod
    def _collect_asset_css_paths(asset_import: dict[str, Any]) -> list[str]:
        paths: list[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())
            elif isinstance(value, list):
                for item in value:
                    add(item)
            elif isinstance(value, dict):
                for key in (
                    "css",
                    "css_path",
                    "css_paths",
                    "css_import",
                    "imports",
                    "styles",
                    "style_paths",
                    "href",
                    "app_css",
                ):
                    add(value.get(key))

        for key in ("css", "css_path", "css_paths", "css_import", "imports", "styles", "style_paths", "app_css"):
            add(asset_import.get(key))
        add(asset_import.get("usage"))
        add(asset_import.get("paths"))
        manifest = asset_import.get("manifest")
        if isinstance(manifest, dict):
            add(manifest)
        elif isinstance(manifest, list):
            add(manifest)
        elif isinstance(manifest, str) and manifest.strip().endswith(".css"):
            add(manifest)
        seen: set[str] = set()
        unique: list[str] = []
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            unique.append(path)
        return unique

    @staticmethod
    def _format_resources_inline(resources: dict[str, Any]) -> str:
        parts: list[str] = []
        fonts = resources.get("fonts") if isinstance(resources.get("fonts"), list) else []
        font_names = [
            str(item.get("family") or item.get("name") or "").strip()
            for item in fonts
            if isinstance(item, dict) and str(item.get("family") or item.get("name") or "").strip()
        ]
        if font_names:
            parts.append("字体 " + " / ".join(font_names))
        icons = resources.get("icons") if isinstance(resources.get("icons"), list) else []
        icon_names = [
            str(item.get("name") or item.get("family") or "").strip()
            for item in icons
            if isinstance(item, dict) and str(item.get("name") or item.get("family") or "").strip()
        ]
        if icon_names:
            parts.append("图标 " + " / ".join(icon_names))
        motion = resources.get("motion") if isinstance(resources.get("motion"), dict) else {}
        if motion:
            parts.append("动效 " + str(motion.get("type") or motion.get("source") or "CSS motion"))
        return "；".join(parts)

    @staticmethod
    def _format_unready_asset_resources(asset_import: dict[str, Any], *, fallback_resources: dict[str, Any]) -> str:
        items: list[str] = []
        manifest_resources = asset_import.get("resources") if isinstance(asset_import.get("resources"), list) else []
        for resource in manifest_resources:
            if not isinstance(resource, dict):
                continue
            status = str(resource.get("status") or "").lower()
            if status not in {"failed", "pending", "error"}:
                continue
            label = str(resource.get("family") or resource.get("id") or resource.get("type") or "").strip()
            if not label:
                continue
            reason = str(resource.get("reason") or "").strip()
            if reason:
                items.append(f"{label}（{status}: {reason}）")
            else:
                items.append(f"{label}（{status}）")
        return "；".join(items) or InteractionDesignService._format_resources_inline(fallback_resources)

    @staticmethod
    def _format_resource_block(resources: dict[str, Any]) -> str:
        lines = ["## 资源声明", ""]
        fonts = resources.get("fonts") if isinstance(resources.get("fonts"), list) else []
        lines.append("### Fonts")
        if fonts:
            for item in fonts:
                if not isinstance(item, dict):
                    continue
                family = str(item.get("family") or item.get("name") or "").strip()
                if family:
                    source = str(item.get("source") or "declared")
                    usage = str(item.get("usage") or "primary-ui")
                    lines.append(f"- `{family}` | source: `{source}` | usage: `{usage}`")
        else:
            lines.append("- 无")
        icons = resources.get("icons") if isinstance(resources.get("icons"), list) else []
        lines.extend(["", "### Icons"])
        if icons:
            for item in icons:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("family") or "").strip()
                if name:
                    source = str(item.get("source") or "declared")
                    usage = str(item.get("usage") or "ui-icons")
                    lines.append(f"- `{name}` | source: `{source}` | usage: `{usage}`")
        else:
            lines.append("- 无")
        motion = resources.get("motion") if isinstance(resources.get("motion"), dict) else {}
        lines.extend(["", "### Motion"])
        if motion:
            for key in ("type", "source", "duration", "easing", "notes"):
                value = str(motion.get(key) or "").strip()
                if value:
                    lines.append(f"- `{key}`: {value}")
        else:
            lines.append("- 无")
        return "\n".join(lines).strip()

    @staticmethod
    def _format_asset_import_block(asset_import: dict[str, Any]) -> str:
        lines = ["## 资源导入结果", ""]
        if not asset_import:
            lines.append("- 状态：`pending`")
            return "\n".join(lines).strip()
        lines.append(f"- 状态：`{asset_import.get('status', 'pending')}`")
        lines.append(f"- 成功：`{InteractionDesignService._asset_import_succeeded(asset_import)}`")
        css_paths = InteractionDesignService._collect_asset_css_paths(asset_import)
        if css_paths:
            lines.append("- 本地 CSS：" + " / ".join(f"`{path}`" for path in css_paths))
        error = str(asset_import.get("error") or asset_import.get("message") or "").strip()
        if error:
            lines.append(f"- 错误：{error}")
        return "\n".join(lines).strip()

    @staticmethod
    def _should_keep_current_default(text: str) -> bool:
        lowered = str(text or "").lower()
        return any(keyword in lowered for keyword in _STYLE_KEEP_CURRENT_KEYWORDS)

    @staticmethod
    def _load_catalog() -> list[dict[str, Any]]:
        if not _CATALOG_PATH.exists():
            return []
        try:
            payload = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = payload.get("systems") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _design_system_path(project_root: Path) -> Path:
        return project_root / ".vizo" / "design-system.json"

    @staticmethod
    def _design_memories_dir(project_root: Path) -> Path:
        return project_root / ".serena" / "memories" / "design"

    def _load_task_design_candidate(self, *, task_dir: Path, state: dict[str, Any]) -> dict[str, Any] | None:
        selected_path = task_dir / "outputs" / "03-selected-system.json"
        if not selected_path.exists():
            return None
        try:
            payload = json.loads(selected_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        candidate = self._normalize_candidate(payload, index=1)
        if not candidate:
            return None
        source = dict(candidate.get("source") or {})
        source.update(
            {
                "kind": "interaction_design_task",
                "manifest": str(_MANIFEST_PATH),
                "task_id": str(state.get("id") or ""),
                "workflow_id": str(state.get("workflow_id") or ""),
                "task_dir": str(task_dir),
                "output_file": str(selected_path),
                "completed_at": str(state.get("completed_at") or state.get("created_at") or ""),
            }
        )
        candidate["source"] = source
        return candidate

    @staticmethod
    def _get_preview_state(session) -> dict[str, Any] | None:
        payload = dict((session.metadata or {}).get(_PREVIEW_STATE_KEY) or {})
        return payload if payload else None

    @staticmethod
    def _set_preview_state(session, payload: dict[str, Any]) -> None:
        session.metadata[_PREVIEW_STATE_KEY] = payload

    @staticmethod
    def _clear_preview_state(session) -> None:
        session.metadata.pop(_PREVIEW_STATE_KEY, None)

    @staticmethod
    def _get_candidate_round(session) -> dict[str, Any] | None:
        payload = dict((session.metadata or {}).get(_CANDIDATE_ROUND_KEY) or {})
        return payload if payload else None

    @staticmethod
    def _set_candidate_round(session, payload: dict[str, Any]) -> None:
        session.metadata[_CANDIDATE_ROUND_KEY] = payload

    @staticmethod
    def _clear_candidate_round(session) -> None:
        session.metadata.pop(_CANDIDATE_ROUND_KEY, None)

    @staticmethod
    def _render_design_memory_files(candidate: dict[str, Any], *, replace_existing: bool) -> dict[str, str]:
        label = str(candidate.get("label") or "")
        summary = str(candidate.get("summary") or "")
        rationale = str(candidate.get("rationale") or "")
        updated_at = str(candidate.get("adopted_at") or utcnow_iso())
        design_spec = dict(candidate.get("design_spec") or {})
        ui_tokens = dict(candidate.get("ui_tokens") or {})
        resources = (
            candidate.get("resources")
            if isinstance(candidate.get("resources"), dict)
            else InteractionDesignService._normalize_resources({}, ui_tokens=ui_tokens)
        )
        asset_import = candidate.get("asset_import") if isinstance(candidate.get("asset_import"), dict) else {}
        resource_summary = InteractionDesignService._format_resources_inline(resources) or "字体、图标和 CSS motion 声明"
        import_status = str(asset_import.get("status") or "pending")
        import_success = InteractionDesignService._asset_import_succeeded(asset_import)
        keywords = [str(item).strip() for item in (candidate.get("keywords") or ["无"]) if str(item).strip()]
        keyword_lines = "\n- ".join(keywords or ["无"])
        replace_text = "本次更新用于替换此前的项目默认视觉系统。" if replace_existing else "这是当前项目首次明确采纳的视觉系统。"
        index_md = (
            "# Design Index\n\n"
            f"- 当前默认视觉系统：`{label}`\n"
            f"- 更新时间：`{updated_at}`\n"
            f"- 更新说明：{replace_text}\n\n"
            "## 资源状态\n"
            f"- 资源声明：{resource_summary}\n"
            f"- 导入状态：`{import_status}`\n"
            f"- 本地导入成功：`{import_success}`\n\n"
            "## 建议读取顺序\n"
            "1. `design/visual-system`\n"
            "2. `design/design-spec`\n"
            "3. `design/ui-tokens`\n"
        )
        visual_system_md = (
            "# Visual System\n\n"
            f"## 当前默认方案\n- 名称：`{label}`\n- 摘要：{summary}\n- 采纳时间：`{updated_at}`\n\n"
            f"## 采纳原因\n{rationale}\n\n"
            f"## 色彩方向\n{candidate.get('color_direction', '')}\n\n"
            f"## 字体方向\n{candidate.get('typography_direction', '')}\n\n"
            f"## 组件气质\n{candidate.get('component_direction', '')}\n\n"
            f"## 动效方向\n{candidate.get('motion_direction', '')}\n\n"
            f"## 风格关键词\n- {keyword_lines}\n\n"
            f"{InteractionDesignService._format_resource_block(resources)}\n\n"
            f"{InteractionDesignService._format_asset_import_block(asset_import)}\n"
        )
        design_spec_md = (
            "# Design Spec\n\n"
            f"## 布局与密度\n- {design_spec.get('layout', '保持页面结构清晰，优先稳定信息层级。')}\n"
            f"- {design_spec.get('density', '默认采用中等密度，不要用过大的留白稀释信息。')}\n\n"
            f"## 字号与标题层级\n- {design_spec.get('type_scale', '正文从稳健基线字号开始，标题只在关键区块抬升。')}\n\n"
            f"## 圆角与表面层级\n- {design_spec.get('radius_policy', '圆角收敛，不使用夸张胶囊化外观。')}\n\n"
            f"## 组件规则\n- {design_spec.get('component_rules', '按钮、卡片、表单和导航都要服从统一的表面层级。')}\n\n"
            f"## 交互规则\n- {design_spec.get('interaction_rules', '动效轻量，反馈明确，不依赖大幅位移制造质感。')}\n"
        )
        token_lines = [
            "# UI Tokens",
            "",
            "## Base Tokens",
        ]
        for key in (
            "font_family",
            "font_size_base",
            "font_size_title",
            "radius_sm",
            "radius_md",
            "radius_lg",
            "primary",
            "surface",
            "border",
            "shadow",
        ):
            value = str(ui_tokens.get(key) or "").strip()
            if value:
                token_lines.append(f"- `{key}`: `{value}`")
        token_lines.extend(
            [
                "",
                "## 使用规则",
                "- 默认先复用这些 token，再考虑页面级局部强化。",
                "- 任何新页面都不要擅自放大字号、圆角或阴影。",
                "- 如果资源导入成功，前端入口优先 import 本地 `src/styles/vizo-design.css` 或资源 manifest 中给出的 CSS。",
                "- 不要为了字体、图标或动效改用远程 CDN；缺失资源应按 `visual-system.md` 的资源声明补齐。",
                "",
                InteractionDesignService._format_asset_import_block(asset_import),
            ]
        )
        return {
            "index.md": index_md.strip() + "\n",
            "visual-system.md": visual_system_md.strip() + "\n",
            "design-spec.md": design_spec_md.strip() + "\n",
            "ui-tokens.md": "\n".join(token_lines).strip() + "\n",
        }
