"""
AgentHub 语义路由器

三级路由：@ 显式指定 → 项目推断 → Function Calling（Haiku）
将用户请求分发到 AgentHub 模块或 Orchestrator。
"""

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class AgentRouter:
    def __init__(self, config: dict):
        self.config = config

    async def route(self, user_request: str, project_config: dict = None) -> dict:
        """三级路由：@ 显式指定 → 项目推断 → Function Calling"""
        project_config = project_config or {}
        project_type = project_config.get("type", "dev")

        # 第一级：@ 显式指定（零成本）
        at_result = self._parse_at_syntax(user_request)
        if at_result:
            module_id, clean_text = at_result
            modules = self._load_available_modules()
            module_ids = {m["id"] for m in modules}
            if module_id not in module_ids:
                return {"target": "error", "error": "module_not_found",
                        "module_id": module_id, "available": list(module_ids)}
            return {"target": "agent_hub", "module_id": module_id,
                    "request": clean_text, "reason": "显式指定"}

        # 第二级：hub 项目 + 唯一模块推断（零成本）
        if project_type == "hub":
            default_module = project_config.get("default_module")
            if default_module:
                modules = self._load_available_modules()
                module_ids = {m["id"] for m in modules}
                if default_module in module_ids:
                    return {"target": "agent_hub", "module_id": default_module,
                            "request": user_request, "reason": "项目推断"}

        # 第三级：Function Calling 语义路由
        modules = self._load_available_modules()
        tools = self._build_router_tools(modules, project_type)

        if not tools:
            # hub 项目无可用模块
            if project_type == "hub":
                return {"target": "error", "error": "no_modules",
                        "reason": "hub 项目无已安装模块"}
            # dev 项目无 hub 模块，直接走 Orchestrator
            return {"target": "orchestrator", "reason": "无 AgentHub 模块"}

        try:
            result = await asyncio.wait_for(
                self._route_via_function_calling(user_request, project_config, tools),
                timeout=20.0
            )
            return result
        except asyncio.TimeoutError:
            logger.warning("语义路由超时（20s）")
            if project_type == "hub":
                return {"target": "error", "error": "timeout",
                        "reason": "路由超时且 hub 项目不允许回退 Orchestrator"}
            return {"target": "error", "error": "timeout",
                    "reason": "路由超时"}
        except Exception as e:
            logger.warning(f"语义路由异常: {e}")
            if project_type == "hub":
                return {"target": "error", "error": "route_error", "reason": str(e)}
            return {"target": "error", "error": "api_call_failed",
                    "reason": str(e)}

    def _parse_at_syntax(self, text: str) -> tuple | None:
        """解析 @module_id，返回 (module_id, clean_text) 或 None"""
        match = re.search(r'@(\w+)', text)
        if match:
            module_id = match.group(1)
            clean_text = text[:match.start()].strip() + " " + text[match.end():].strip()
            return module_id, clean_text.strip()
        return None

    def _load_available_modules(self) -> list[dict]:
        """扫描 agents/_builtin/ 和 agents/_user/ 下所有 manifest.json

        _builtin 结构: agents/_builtin/{module_id}/manifest.json
        _user 结构:    agents/_user/{namespace}/{module_id}/manifest.json
        """
        modules = []
        base_dir = Path(__file__).parent / "agents"

        # _builtin: 单层扫描
        builtin_dir = base_dir / "_builtin"
        if builtin_dir.exists():
            for module_dir in sorted(builtin_dir.iterdir()):
                if not module_dir.is_dir():
                    continue
                self._try_load_manifest(module_dir, "_builtin", modules)

        # _user: 两层扫描 (_user/{namespace}/{module_id}/)
        user_dir = base_dir / "_user"
        if user_dir.exists():
            for namespace_dir in sorted(user_dir.iterdir()):
                if not namespace_dir.is_dir() or namespace_dir.name.startswith('.'):
                    continue
                for module_dir in sorted(namespace_dir.iterdir()):
                    if not module_dir.is_dir() or module_dir.name.startswith('.'):
                        continue
                    self._try_load_manifest(module_dir, "_user", modules)

        return modules

    def _try_load_manifest(self, module_dir: Path, source: str, modules: list):
        """尝试加载单个模块的 manifest.json"""
        manifest_file = module_dir / "manifest.json"
        if not manifest_file.exists():
            return
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            modules.append({
                "id": manifest["id"],
                "manifest": manifest,
                "dir": str(module_dir),
                "source": source
            })
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"加载模块失败 {module_dir}: {e}")

    def _build_router_tools(self, modules: list, project_type: str) -> list:
        """从已安装模块的 manifest 自动生成 Function Calling tool 定义"""
        tools = []
        for module in modules:
            manifest = module["manifest"]
            for wf_id, wf in manifest.get("workflows", {}).items():
                wf_desc = wf.get("description", wf.get("name", wf_id))
                tools.append({
                    "name": f"{manifest['id']}__{wf_id}",
                    "description": f"{manifest['name']} - {wf.get('name', wf_id)}。{wf_desc}",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "选择此模块的理由（一句话）"
                            }
                        },
                        "required": ["reason"]
                    }
                })

        # hub 项目不生成 dev_task（架构级隔离）
        if project_type != "hub":
            tools.append({
                "name": "dev_task",
                "description": "软件开发任务：编写代码、修复 bug、重构、部署等涉及代码仓库的工作",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "选择开发任务的理由"}
                    },
                    "required": ["reason"]
                }
            })

        return tools

    @staticmethod
    def _read_settings_auth() -> tuple:
        """从 ~/.claude/settings.json 读取认证信息 + 受管 env。"""
        try:
            import json as _json
            from lib.settings_handler import extract_managed_main_session_env
            settings_path = Path.home() / ".claude" / "settings.json"
            if not settings_path.exists():
                return ("", "", {})
            settings = _json.loads(settings_path.read_text(encoding="utf-8"))
            env = settings.get("env", {})
            api_key = env.get("ANTHROPIC_AUTH_TOKEN", "") or env.get("ANTHROPIC_API_KEY", "")
            base_url = env.get("ANTHROPIC_BASE_URL", "")
            return (api_key, base_url, extract_managed_main_session_env(env))
        except Exception:
            return ("", "", {})

    async def _route_via_function_calling(self, user_request: str,
                                           project_config: dict, tools: list) -> dict:
        """调用 Anthropic API 做语义路由（Function Calling 模式）"""
        import urllib.request
        import json as _json

        # 认证优先级：settings.json → config.json → .env
        api_key, base_url, current_env = AgentRouter._read_settings_auth()

        if not api_key:
            ext_anthropic = self.config.get("external_models", {}).get("anthropic", {})
            api_key = ext_anthropic.get("api_key", "")
            base_url = base_url or ext_anthropic.get("base_url", "")
            if not current_env:
                from lib.settings_handler import extract_managed_main_session_env
                current_env = extract_managed_main_session_env(ext_anthropic.get("env", {}))

        if not api_key:
            from lib.config_loader import read_main_session_api_key_from_env_file
            api_key = read_main_session_api_key_from_env_file()

        if not api_key:
            return {"target": "error", "error": "api_unreachable",
                    "reason": "未找到 API Key（settings.json / config.json / .env）"}

        # system prompt 简化为角色描述
        system_prompt = "你是一个任务路由器。根据用户请求，选择最合适的处理工具。"
        if project_config.get("description"):
            system_prompt += f"\n当前项目背景：{project_config['description']}"

        # 使用 Function Calling（tools + tool_choice）
        from lib.settings_handler import get_main_session_api_model
        url = (base_url.rstrip("/") + "/v1/messages") if base_url else "https://api.anthropic.com/v1/messages"
        payload = _json.dumps({
            "model": get_main_session_api_model(base_url, current_env=current_env, prefer="opus"),
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_request}],
            "tools": tools,
            "tool_choice": {"type": "any"},
        }).encode("utf-8")

        # 认证头：ANTHROPIC_AUTH_TOKEN 用 Authorization Bearer，API_KEY 用 x-api-key
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if api_key.startswith("sk-ant-"):
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(url, data=payload, headers=headers)

        loop = asyncio.get_event_loop()
        try:
            def _do_request():
                resp = urllib.request.urlopen(req, timeout=15)
                return _json.loads(resp.read().decode("utf-8"))

            response = await loop.run_in_executor(None, _do_request)
        except Exception as e:
            logger.warning(f"语义路由 API 调用失败: {e}")
            return {"target": "error", "error": "api_unreachable",
                    "reason": str(e)}

        # 从响应中提取 tool_use block
        tool_name = ""
        tool_input = {}
        for block in response.get("content", []):
            if block.get("type") == "tool_use":
                tool_name = block["name"]
                tool_input = block.get("input", {})
                break
        else:
            # 无 tool_use block（代理非标行为）
            return {"target": "error", "error": "api_unreachable",
                    "reason": "响应中无 tool_use block"}

        reason = tool_input.get("reason", "")

        if not tool_name or tool_name == "dev_task":
            return {"target": "orchestrator", "reason": reason or "routed_to_dev"}

        try:
            module_id, workflow_id = tool_name.split("__", 1)
        except ValueError:
            logger.warning(f"路由 tool_name 解析失败: {tool_name}")
            return {"target": "error", "error": "api_unreachable",
                    "reason": f"tool_name 格式错误: {tool_name}"}

        return {
            "target": "agent_hub",
            "module_id": module_id,
            "workflow_id": workflow_id,
            "request": user_request,
            "reason": reason
        }
