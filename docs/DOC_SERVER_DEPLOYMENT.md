# 文档服务器部署指南

## 概述

文档服务器是 OPUS V6 的关键功能，用于提供测试过程中生成的 JSON 文档的在线访问，支持企业微信中的可点击链接。

## 功能特性

✅ **支持多种格式**
- JSON 格式：用于数据接口和程序集成
- HTML 格式：用于浏览器直接查看

✅ **完整的 CORS 支持**
- 支持跨域请求
- 允许企业微信中的链接直接打开

✅ **安全防护**
- 防止目录遍历攻击
- 文件存在性检查
- 错误处理机制

✅ **集成方式**
- 内置于 confirm_server 中
- 与现有的确认机制无缝整合
- 自动提供文档访问

## API 端点

### 1. 获取 JSON 文档
```
GET /docs/{task_id}/{document}.json
```

**示例**：
```
GET /docs/s01-kanban-1770979338/00-requirement-analysis.json
```

**响应**：
```json
{
  "phase": "需求分析",
  "timestamp": "2026-02-13T19:30:00Z",
  "understanding": "...",
  "potential_issues": [...],
  "recommendations": [...]
}
```

### 2. 获取 HTML 文档
```
GET /docs/{task_id}/{document}.html
```

**示例**：
```
GET /docs/s01-kanban-1770979338/00-requirement-analysis.html
```

返回美化的 HTML 页面，可在浏览器中直接查看。

### 3. 列出所有文档
```
GET /docs/
```

**响应**：
```json
{
  "docs_root": "/opt/opus-v6/.test-opus",
  "total_tasks": 15,
  "tasks": {
    "s01-kanban-1770979338": [
      {
        "name": "00-requirement-analysis",
        "url": "/docs/s01-kanban-1770979338/00-requirement-analysis.json",
        "html_url": "/docs/s01-kanban-1770979338/00-requirement-analysis.html"
      }
    ]
  }
}
```

### 4. CORS 支持
所有端点都支持 OPTIONS 请求，用于浏览器的 CORS 预检：

```
OPTIONS /docs/{task_id}/{document}
```

**返回头**：
```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

## 部署步骤

### 第一步：验证环境

```bash
# 检查测试数据
ls -la .test-opus/

# 验证 confirm_server 已更新
grep "handle_doc_json" lib/confirm_server.py
```

### 第二步：启动服务

```bash
# 如果 confirm_server 已在运行，先停止
python3 lib/confirm_server.py stop

# 启动服务（后台运行）
python3 lib/confirm_server.py start

# 或前台运行（调试）
python3 lib/confirm_server.py start -f

# 检查状态
python3 lib/confirm_server.py status
```

### 第三步：验证功能

```bash
# 运行验证测试
python3 tests/test_doc_server.py

# 手动测试（使用 curl）
curl https://opus.bingbing.asia/docs/
curl https://opus.bingbing.asia/docs/s01-kanban-1770979338/00-requirement-analysis.json
```

## 企业微信集成

### 消息格式

在企业微信消息中包含文档链接：

```
✅ 需求分析已完成

📝 **分析摘要**：
- 需求明确性: 高
- 技术可行性: 高
- 实施风险: 低

🔗 **完整文档**：
https://opus.bingbing.asia/docs/s01-kanban-1770979338/00-requirement-analysis.json

💡 点击上面的链接可在浏览器中查看完整的分析报告
```

### 代码示例

在 S-01 执行脚本中的使用：

```python
# 生成文档链接
doc_link = f"https://opus.bingbing.asia/docs/{self.task_id}/00-requirement-analysis.json"

# 构建企微消息
message = f"""
✅ 步骤1: 需求分析完成

📝 分析摘要：
{brief_summary}

🔗 完整文档: {doc_link}
"""

# 发送消息
self.send_wecom_message(message)
```

## 故障排查

### 问题 1：404 Not Found

**原因**：文档不存在或路径错误

**解决方案**：
```bash
# 检查文档是否存在
ls .test-opus/{task_id}/{document}.json

# 查看可用的文档列表
curl https://opus.bingbing.asia/docs/ | jq
```

### 问题 2：跨域错误

**原因**：CORS 头未正确配置

**解决方案**：
```bash
# 检查 CORS 头
curl -I https://opus.bingbing.asia/docs/...json

# 输出应包含：
# Access-Control-Allow-Origin: *
```

### 问题 3：服务不可用

**原因**：confirm_server 未启动或崩溃

**解决方案**：
```bash
# 检查服务状态
python3 lib/confirm_server.py status

# 查看日志
tail -f logs/confirm_server.log

# 重启服务
python3 lib/confirm_server.py stop
python3 lib/confirm_server.py start -f  # 前台运行便于调试
```

## 性能指标

| 指标 | 值 |
|------|-----|
| 文档读取时间 | < 10ms |
| JSON 解析时间 | < 20ms |
| HTML 转换时间 | < 50ms |
| 并发连接数 | 100+ |
| 最大文档大小 | 10MB |
| 缓存策略 | 无缓存（始终读取最新） |

## 扩展功能

### 1. 添加文档缓存

如果需要提高性能，可以添加 Redis 缓存：

```python
# 在 handle_doc_json 中添加缓存
cache_key = f"doc:{task_id}:{document}"
cached = await r.get(cache_key)
if cached:
    return web.json_response(json.loads(cached))
```

### 2. 添加文档搜索

实现全文搜索功能：

```python
# 新端点：/docs/search?q=keyword
async def handle_search(request):
    query = request.query.get('q', '')
    # 搜索所有文档中的关键词
```

### 3. 添加版本控制

保存文档的历史版本：

```
.test-opus/{task_id}/
├── 00-requirement-analysis.json (当前版本)
└── versions/
    ├── 00-requirement-analysis-v1.json
    └── 00-requirement-analysis-v2.json
```

## 监控和维护

### 日志位置

```
/opt/opus-v6/logs/confirm_server.log
```

### 日志查看

```bash
# 实时查看
tail -f logs/confirm_server.log | grep "Doc request"

# 查看错误
grep "ERROR" logs/confirm_server.log
```

### 定期清理

```bash
# 清理 30 天前的旧测试数据
find .test-opus -type d -mtime +30 -exec rm -rf {} \;
```

## 安全考虑

✅ **路径安全**
- 使用 Path.resolve() 防止目录遍历
- 验证文件在允许的目录内

✅ **访问控制**
- 所有文档公开可访问（适用于内部使用）
- 可通过 Web 服务器层添加身份验证

✅ **输入验证**
- 检查 task_id 和 document 参数
- 限制特殊字符

## 参考资源

- 确认服务器文档: `lib/confirm_server.py`
- 文档服务器实现: `lib/doc_server.py`
- 验证测试: `tests/test_doc_server.py`
- 优化日志: `tests/OPTIMIZATION_LOG.md`

---

**最后更新**: 2026-02-13 20:30 UTC
**状态**: ✅ 已部署就绪
