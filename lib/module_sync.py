"""manifest 到 CLAUDE.md 自动同步模块

扫描 agents/ 下所有 manifest.json，生成模块摘要表格写入 CLAUDE.md。
hash 一致时跳过写入（≤10ms），保证频繁调用无性能影响。
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

HASH_MARKER = r'<!-- AGENT_MODULES_HASH:(\w+) -->'
START_MARKER = '<!-- AGENT_MODULES_START -->'
END_MARKER = '<!-- AGENT_MODULES_END -->'


def sync_modules_to_claude_md(project_root: str) -> None:
    """同步模块信息到 CLAUDE.md（静默失败，不影响主流程）"""
    try:
        _do_sync(project_root)
    except Exception as e:
        logger.debug(f"module_sync 静默失败: {e}")


def _do_sync(project_root: str) -> None:
    root = Path(project_root)
    claude_md = root / "CLAUDE.md"
    if not claude_md.exists():
        return

    # 1. 扫描模块
    modules = _scan_modules(root)

    # 2. 计算 hash
    data_for_hash = json.dumps(
        sorted(modules, key=lambda m: m["id"]),
        ensure_ascii=False, sort_keys=True
    )
    new_hash = hashlib.md5(data_for_hash.encode()).hexdigest()[:8]

    # 3. 读取 CLAUDE.md 中的已存 hash
    content = claude_md.read_text(encoding="utf-8")
    existing_hash_match = re.search(HASH_MARKER, content)
    if existing_hash_match and existing_hash_match.group(1) == new_hash:
        return  # hash 一致，跳过

    # 4. 生成新表格
    table_content = _build_table_section(modules, new_hash)

    # 5. 替换或追加
    if existing_hash_match and END_MARKER in content:
        # 替换：从 HASH 行到 END 标记
        pattern = re.compile(
            r'<!-- AGENT_MODULES_HASH:\w+ -->.*?' + re.escape(END_MARKER),
            re.DOTALL
        )
        new_content = pattern.sub(table_content, content)
    else:
        # 首次或标记残缺：末尾追加完整段落
        new_content = content.rstrip('\n') + '\n\n' + _build_full_section(modules, new_hash)

    # 6. 原子写入
    tmp = str(claude_md) + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new_content)
    os.rename(tmp, str(claude_md))


def _scan_modules(root: Path) -> list:
    """扫描 agents/_builtin/ 和 agents/_user/ 下所有 manifest.json"""
    modules = []
    agents_dir = root / "agents"
    for search_dir in [agents_dir / "_builtin", agents_dir / "_user"]:
        if not search_dir.exists():
            continue
        for module_dir in sorted(search_dir.iterdir()):
            if not module_dir.is_dir():
                continue
            manifest_file = module_dir / "manifest.json"
            if not manifest_file.exists():
                continue
            try:
                m = json.loads(manifest_file.read_text(encoding="utf-8"))
                desc = m.get("description", "")
                if len(desc) > 100:
                    desc = desc[:97] + "..."
                workflows = {}
                for wf_id, wf in m.get("workflows", {}).items():
                    workflows[wf_id] = wf.get("name", wf_id)
                modules.append({
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "description": desc,
                    "workflows": workflows,
                })
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"module_sync: 跳过损坏的 manifest {manifest_file}: {e}")
    return modules


def _build_table_section(modules: list, hash_val: str) -> str:
    """生成表格段落（HASH 行到 END 标记）"""
    lines = [
        f'<!-- AGENT_MODULES_HASH:{hash_val} -->',
        START_MARKER,
        '| 模块 ID | 名称 | 描述 | 工作流 |',
        '|---------|------|------|--------|',
    ]
    if modules:
        for m in modules:
            wf_str = " / ".join(f"{k}: {v}" for k, v in m["workflows"].items())
            lines.append(f'| {m["id"]} | {m["name"]} | {m["description"]} | {wf_str} |')
    else:
        lines.append('| (暂无已安装模块) | — | — | — |')
    lines.append(END_MARKER)
    return '\n'.join(lines)


def _build_full_section(modules: list, hash_val: str) -> str:
    """生成完整段落（含标题和说明文字）"""
    table = _build_table_section(modules, hash_val)
    return f"""## Agent 模块语义路由

opus 系统除了开发任务，还支持专业 Agent 模块。用户的所有工作类请求都应通过 opus 命令执行。

当 opus 报告"语义路由 API 不可用"时，根据下表判断用户请求匹配哪个模块，加 `@模块ID` 重新执行。
不匹配任何模块的请求不加 `@`，走默认开发流。

{table}

示例：`opus "审查这份合同的风险" @contract_service`
"""
