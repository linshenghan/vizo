"""Agent 模块创建核心函数：manifest 校验、磁盘写入、数量保护。

提供写入前校验（区别于 agent_hub.py 的运行时校验），
以及模块创建/复制/骨架生成等磁盘操作。
"""

import fcntl
import json
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────

REQUIRED_FIELDS = ("id", "name", "description", "workflows", "roles")
ID_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"
ALLOWED_TOOLS = frozenset({
    "Read", "Write", "Edit", "Glob", "Grep",
    "WebSearch", "WebFetch", "NotebookEdit",
})
USER_MODULE_DIR = "default"  # agents/_user/ 下的子目录名

SKELETON_MANIFEST = {
    "id": "my_agent",
    "name": "我的 Agent",
    "icon": "🔧",
    "description": "请修改此描述，详细说明模块功能和适用场景（20~200字符）",
    "version": "1.0",
    "author": "user",
    "workflows": {
        "default": {
            "name": "默认工作流",
            "description": "工作流描述",
            "steps": [
                {
                    "step": "step_1",
                    "role": "my_role",
                    "output": "01-output.md",
                    "confirm": True,
                    "description": "步骤描述",
                }
            ],
        }
    },
    "roles": {
        "my_role": {
            "template": "roles/my_role.md",
            "model": "sonnet",
            "tools": ["Read", "Write"],
        }
    },
}

SKELETON_ROLE = """\
# 角色：我的角色

## 角色定位
一句话定义角色身份和专业领域。

## 任务指令
1. 第一步操作
2. 第二步操作

## 输出格式
- 输出文件结构要求

## 质量标准
- 质量要求
"""

SKELETON_README = """\
# 模块开发说明

## manifest.json 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 模块唯一标识，小写字母/数字/下划线，2~32 字符 |
| `name` | 是 | 模块展示名称，1~20 字符 |
| `description` | 是 | 模块功能描述，供语义路由匹配，建议 20~200 字符 |
| `icon` | 否 | 单个 emoji，默认 "🔧" |
| `version` | 否 | 版本号，默认 "1.0" |
| `author` | 否 | 作者，默认 "user" |
| `workflows` | 是 | 至少一个工作流，每个含 `steps` 数组 |
| `roles` | 是 | 角色定义，`template` 指向 roles/ 下的 .md 文件 |

### steps 字段

| 字段 | 说明 |
|------|------|
| `step` | 步骤唯一 ID |
| `role` | 对应 roles 中的角色名 |
| `output` | 输出文件名 |
| `confirm` | 是否需要用户确认（true/false） |
| `description` | 步骤描述 |

### roles 字段

| 子字段 | 说明 |
|--------|------|
| `template` | 角色模板文件路径（必须以 `roles/` 开头） |
| `model` | 模型名（sonnet / opus） |
| `tools` | 工具列表（如 Read, Write，禁止 Bash） |

## 角色模板（roles/*.md）

每个角色模板包含四部分：
1. **角色定位** — 一句话定义身份和专业领域
2. **任务指令** — 具体操作步骤
3. **输出格式** — 输出文件结构要求
4. **质量标准** — 质量检查项

## 使用方法

修改完成后执行 `opus agents` 确认模块已加载。
使用方法：`opus "你的请求" @模块ID`
"""


# ── 内部辅助 ──────────────────────────────────────────

def _get_agents_base_dir() -> Path:
    """返回 agents/ 根目录的绝对路径。"""
    return Path(__file__).resolve().parent.parent / "agents"


def _flatten_steps(workflow: dict) -> list:
    """将 stages 嵌套结构展开为扁平 steps 列表。"""
    if "stages" in workflow:
        steps = []
        for stage in workflow["stages"]:
            steps.extend(stage.get("steps", []))
        return steps
    return workflow.get("steps", [])


def _list_all_module_ids() -> list[str]:
    """列出所有已有模块 ID（_builtin + _user）。"""
    base_dir = _get_agents_base_dir()
    ids = []
    for search_name in ["_builtin", "_user"]:
        search_dir = base_dir / search_name
        if not search_dir.exists():
            continue
        for manifest_file in search_dir.rglob("manifest.json"):
            ids.append(manifest_file.parent.name)
    return sorted(set(ids))


# ── 公开接口 ──────────────────────────────────────────

def validate_manifest(manifest_data: dict, module_dir: Path = None) -> dict:
    """在模块写入磁盘前执行完整格式校验。

    Args:
        manifest_data: 待校验的 manifest 字典。
        module_dir: 若非 None，校验模板文件存在性（磁盘上已有模块场景）。

    Returns:
        {"valid": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    has_bash_warning = False

    # V1: 必填字段
    for field in REQUIRED_FIELDS:
        if field not in manifest_data:
            errors.append(f"缺少必填字段: {field}")
    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}

    # V2: id 格式
    if not re.match(ID_PATTERN, manifest_data["id"]):
        errors.append(
            "id 格式不合规：只允许小写字母开头，后跟小写字母、数字、下划线，长度 2~32"
        )

    # V3: name 长度
    name = manifest_data.get("name", "")
    if not (1 <= len(name) <= 20):
        errors.append(f"name 长度不合规：当前 {len(name)} 字符，要求 1~20")

    # V4: description 长度（warning，不阻断）
    desc = manifest_data.get("description", "")
    if not (20 <= len(desc) <= 200):
        warnings.append(
            f"description 长度不理想：当前 {len(desc)} 字符，建议 20~200"
        )

    # V5: workflows 非空
    workflows = manifest_data.get("workflows", {})
    if not workflows:
        errors.append("workflows 不能为空，至少包含 1 个工作流")

    # V6-V8: 遍历 workflows 校验步骤和角色引用
    roles_defined = set(manifest_data.get("roles", {}).keys())
    for wf_id, wf in workflows.items():
        steps = _flatten_steps(wf)
        if not steps:
            errors.append(f"工作流 {wf_id} 没有步骤")
            continue
        for step in steps:
            # V7: 步骤必填字段
            for sf in ("step", "role", "output"):
                if sf not in step:
                    errors.append(f"工作流 {wf_id} 步骤缺少字段: {sf}")
            # V8: 角色引用完整性
            role = step.get("role")
            if role and role not in roles_defined:
                errors.append(f"角色 {role} 在 roles 中未定义（工作流 {wf_id}）")

    # V9-V14: 角色配置校验
    for role_name, role_cfg in manifest_data.get("roles", {}).items():
        template = role_cfg.get("template", "")
        # V11: 绝对路径
        if template.startswith("/"):
            errors.append(
                f"路径安全限制：角色 {role_name} 的 template 不允许使用绝对路径"
            )
        # V9: .. 禁止
        elif ".." in template:
            errors.append(
                f"路径安全限制：角色 {role_name} 的 template 不允许包含 .."
            )
        # V10: 前缀
        elif not template.startswith("roles/"):
            errors.append(f"角色 {role_name} 的 template 必须以 roles/ 开头")

        # V12: 模板文件存在性（仅 module_dir 非 None 时校验）
        if module_dir and template and not (module_dir / template).exists():
            errors.append(f"模板文件不存在: {template}（角色 {role_name}）")

        # V13-V14: 工具校验
        tools = set(role_cfg.get("tools", []))
        if "Bash" in tools:
            has_bash_warning = True
            warnings.append(
                f"⚠ 安全警告：角色 {role_name} 使用了 Bash 工具，存在安全风险"
            )
        invalid_tools = tools - ALLOWED_TOOLS - {"Bash"}
        if invalid_tools:
            warnings.append(
                f"角色 {role_name} 使用了未知工具: {', '.join(sorted(invalid_tools))}"
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "has_bash_warning": has_bash_warning,
    }


def check_module_limit(config: dict) -> dict:
    """检查用户自定义模块数量是否超限。

    Args:
        config: 系统配置（从 config.json 加载）。

    Returns:
        {"allowed": bool, "current_count": int, "max_limit": int,
         "existing_modules": list[dict]}
    """
    max_limit = config.get("max_user_modules", 20)
    base_dir = _get_agents_base_dir() / "_user" / USER_MODULE_DIR

    existing_modules: list[dict] = []
    if base_dir.exists():
        for d in sorted(base_dir.iterdir()):
            if not d.is_dir():
                continue
            manifest_file = d / "manifest.json"
            if manifest_file.exists():
                try:
                    m = json.loads(manifest_file.read_text(encoding="utf-8"))
                    existing_modules.append({
                        "id": m.get("id", d.name),
                        "name": m.get("name", d.name),
                    })
                except (json.JSONDecodeError, OSError):
                    existing_modules.append({"id": d.name, "name": d.name})

    current_count = len(existing_modules)
    allowed = (max_limit == 0) or (current_count < max_limit)

    return {
        "allowed": allowed,
        "current_count": current_count,
        "max_limit": max_limit,
        "existing_modules": existing_modules,
    }


def check_id_conflict(module_id: str) -> dict:
    """检查模块 ID 是否与现有模块冲突。

    Returns:
        {"conflict": bool, "conflict_source": str|None, "conflict_name": str|None}
    """
    base_dir = _get_agents_base_dir()

    for search_name in ["_builtin", "_user"]:
        search_dir = base_dir / search_name
        if not search_dir.exists():
            continue
        for manifest_file in search_dir.rglob("manifest.json"):
            module_dir = manifest_file.parent
            if module_dir.name == module_id:
                try:
                    m = json.loads(manifest_file.read_text(encoding="utf-8"))
                    return {
                        "conflict": True,
                        "conflict_source": search_name,
                        "conflict_name": m.get("name", module_id),
                    }
                except (json.JSONDecodeError, OSError):
                    return {
                        "conflict": True,
                        "conflict_source": search_name,
                        "conflict_name": module_id,
                    }

    return {"conflict": False, "conflict_source": None, "conflict_name": None}


def write_module_to_disk(
    manifest_data: dict, roles: dict, module_id: str = None
) -> Path:
    """将校验通过的 manifest 和角色模板写入 agents/_user/default/ 目录。

    Args:
        manifest_data: 校验通过的 manifest 字典。
        roles: 角色模板字典，key=角色名, value=Markdown 全文。
        module_id: 覆盖 manifest 中的 id（ID 冲突重命名场景）。

    Returns:
        写入的模块目录 Path。
    """
    mid = module_id or manifest_data["id"]
    base_dir = _get_agents_base_dir() / "_user" / USER_MODULE_DIR
    module_path = base_dir / mid

    # 路径安全：确保目标在 _user/default/ 下
    if not str(module_path.resolve()).startswith(str(base_dir.resolve())):
        raise ValueError("路径安全限制：模块路径必须在 agents/_user/default/ 下")

    base_dir.mkdir(parents=True, exist_ok=True)
    is_new = not module_path.exists()

    lock_file = base_dir / ".lock"
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            write_manifest = dict(manifest_data)
            if module_id and module_id != manifest_data.get("id"):
                write_manifest["id"] = mid

            module_path.mkdir(parents=True, exist_ok=True)
            roles_dir = module_path / "roles"
            roles_dir.mkdir(exist_ok=True)

            # 写入 manifest.json
            (module_path / "manifest.json").write_text(
                json.dumps(write_manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # 写入角色模板
            for role_name, role_cfg in write_manifest.get("roles", {}).items():
                template_path = role_cfg.get("template", f"roles/{role_name}.md")
                role_file = module_path / template_path
                role_file.parent.mkdir(parents=True, exist_ok=True)
                content = roles.get(role_name, "")
                if content:
                    role_file.write_text(content, encoding="utf-8")

            logger.info("模块已写入: %s", module_path)
            return module_path
        except Exception as e:
            if is_new and module_path.exists():
                shutil.rmtree(module_path, ignore_errors=True)
            raise RuntimeError(f"写入模块失败: {e}") from e
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def generate_skeleton(module_id: str = "my_agent") -> Path:
    """生成空白骨架模块。

    Args:
        module_id: 模块 ID，默认 "my_agent"。

    Returns:
        生成的模块目录 Path。

    Raises:
        ValueError: ID 格式不合规或目标模块已存在。
    """
    # ID 格式校验
    if not re.match(ID_PATTERN, module_id):
        raise ValueError(f"id 格式不合规：{module_id}")

    # 冲突检查
    conflict = check_id_conflict(module_id)
    if conflict["conflict"]:
        raise ValueError(f"目标模块已存在: {module_id}（来源: {conflict['conflict_source']}）")

    base_dir = _get_agents_base_dir() / "_user" / USER_MODULE_DIR
    module_path = base_dir / module_id
    module_path.mkdir(parents=True, exist_ok=True)

    # 写入 manifest.json
    manifest = dict(SKELETON_MANIFEST)
    manifest["id"] = module_id
    (module_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # 写入示例角色模板
    roles_dir = module_path / "roles"
    roles_dir.mkdir(exist_ok=True)
    (roles_dir / "my_role.md").write_text(SKELETON_ROLE, encoding="utf-8")

    # 写入 README.md 说明文件
    (module_path / "README.md").write_text(SKELETON_README, encoding="utf-8")

    logger.info("骨架模块已生成: %s", module_path)
    return module_path


def copy_module(source_id: str, new_id: str) -> Path:
    """从已有模块复制创建新模块。

    Args:
        source_id: 源模块 ID。
        new_id: 新模块 ID。

    Returns:
        新模块目录 Path。

    Raises:
        ValueError: 新 ID 格式不合规或目标已存在。
        FileNotFoundError: 源模块不存在。
    """
    if not re.match(ID_PATTERN, new_id):
        raise ValueError(f"id 格式不合规：{new_id}")

    # 查找源模块
    base_dir = _get_agents_base_dir()
    source_dir = None
    for search_name in ["_builtin", "_user"]:
        search_dir = base_dir / search_name
        if not search_dir.exists():
            continue
        for manifest_file in search_dir.rglob("manifest.json"):
            if manifest_file.parent.name == source_id:
                source_dir = manifest_file.parent
                break
        if source_dir:
            break

    if not source_dir:
        available = _list_all_module_ids()
        raise FileNotFoundError(
            f"源模块不存在: {source_id}\n"
            f"可用模块: {', '.join(available) if available else '无'}"
        )

    # 复制到 _user/default/
    dest_dir = base_dir / "_user" / USER_MODULE_DIR / new_id
    if dest_dir.exists():
        raise ValueError(f"目标模块已存在: {new_id}")

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, dest_dir)

    # 更新 manifest
    manifest_file = dest_dir / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["id"] = new_id
    manifest["version"] = "1.0"
    manifest["author"] = "user"
    manifest_file.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info("模块已复制: %s → %s", source_id, dest_dir)
    return dest_dir


async def call_agent_creator(config: dict, description: str) -> dict:
    """调用 agent_creator 角色生成模块定义。

    在临时目录中执行 AgentRunner.run()，解析 AI 输出的 JSON。

    Returns:
        {"success": bool, "manifest": dict|None, "roles": dict|None, "error": str|None}
    """
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="opus_agent_creator_")
    tmp_path = Path(tmp_dir)

    try:
        # 1. 写入用户请求到临时目录
        (tmp_path / "user_request.md").write_text(
            f"# 用户请求\n\n{description}\n", encoding="utf-8"
        )

        # 2. 调用 AgentRunner
from vizo_core.agent_runner import AgentRunner
        runner = AgentRunner(config)
        await runner.run(
            role="agent_creator",
            task_dir=tmp_path,
            input_docs={"user_request": description},
            output_file="agent_definition.json",
            model_override="sonnet",
            tools_override=["Read", "Write"],
            cwd=tmp_dir,
            timeout=120,
        )

        # 3. 读取输出文件
        output_file = tmp_path / "agent_definition.json"
        if not output_file.exists():
            return {"success": False, "manifest": None, "roles": None,
                    "error": "AI 未生成输出文件，请重试"}

        raw = output_file.read_text(encoding="utf-8").strip()
        # 处理可能被 markdown 代码块包裹的情况
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            raw = "\n".join(lines)

        data = json.loads(raw)
        manifest = data.get("manifest")
        roles = data.get("roles")

        if not manifest:
            return {"success": False, "manifest": None, "roles": None,
                    "error": "AI 输出缺少 manifest 字段"}
        if not roles:
            return {"success": False, "manifest": None, "roles": None,
                    "error": "AI 输出缺少 roles 字段"}

        return {"success": True, "manifest": manifest, "roles": roles, "error": None}

    except json.JSONDecodeError as e:
        return {"success": False, "manifest": None, "roles": None,
                "error": f"AI 输出的 JSON 格式无效: {e}"}
    except Exception as e:
        logger.warning("call_agent_creator 异常: %s", e)
        return {"success": False, "manifest": None, "roles": None,
                "error": f"生成失败: {e}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
