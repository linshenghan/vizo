# Opus V6 秘书进程诊断指南

## 📋 快速健康检查

### 1️⃣ 检查秘书进程是否运行

```bash
# 查看秘书进程状态
systemctl --user status opus-secretary

# 或手动启动（前台调试）
python3 /opt/opus-v6/secretary.py
```

### 2️⃣ 查看秘书日志

```bash
# 查看最新日志
tail -f /opt/opus-v6/logs/secretary.log

# 查看所有任务日志
ls -lah /opt/opus-v6/logs/
```

### 3️⃣ 检查 Redis 连接

```bash
# 检查 Redis 6379（企微消息）
redis-cli -p 6379 ping
# 期望输出：PONG

# 检查 Redis 6380（确认页面）
redis-cli -p 6380 ping
# 期望输出：PONG

# 查看待处理的消息
redis-cli -p 6379 get "opus:reply:default"
```

---

## 🔧 功能验证步骤

### 场景 A：简单问答

**步骤 1：发送问答消息**
```
在企微发送：Python 是什么
```

**预期行为**：
- ✅ 秘书立即回复：`💬 收到，查询中...`
- ✅ 秘书启动 `opus --chat --wecom "Python 是什么"`
- ✅ 大约 30 秒内收到答案
- ✅ 答案以 `✅ 任务「Python 是什么」已完成` 开头

**日志检查**：
```bash
tail -20 /opt/opus-v6/logs/secretary.log
# 应该看到：
# [INFO] 收到消息: Python 是什么
# [INFO] 启动 opus: python3 /opt/opus-v6/opus.py --chat --wecom...
# [INFO] opus 任务成功完成，摘要长度: 450
```

---

### 场景 B：开发任务

**步骤 1：发送开发任务**
```
在企微发送：添加一个登录功能
```

**预期行为**：
- ✅ 秘书立即回复：`📋 收到！正在启动任务...`
- ✅ 秘书启动 `opus --wecom "添加一个登录功能"`
- ✅ 秘书开始监听确认请求（需求、PRD、架构）
- ✅ 任务完成时收到 `✅ 任务「添加一个登录功能」已完成`

**日志检查**：
```bash
tail -50 /opt/opus-v6/logs/secretary.log | grep -E "(收到消息|启动 opus|成功完成|超时|失败)"
```

---

### 场景 C：任务互斥

**步骤 1-2：快速发送两条消息**
```
企微 1: 添加登录功能
企微 2: 添加支付功能（在第一个任务完成前发送）
```

**预期行为**：
- ✅ 秘书回复第一条：`📋 收到！正在启动任务...`
- ✅ 秘书拒绝第二条：`⏳ 当前正在执行「添加登录功能」...请等待完成后再发新任务，或回复「取消」终止当前任务`

---

### 场景 D：任务超时

**步骤 1：发送复杂任务**
```
企微: 列出所有系统内存泄漏问题
```

**预期行为**：
- 如果任务在 600 秒（10 分钟）内完成 → 显示结果
- 如果任务超时 → 秘书自动终止：`⏰ 任务「列出所有系统内存泄漏问题」执行超时 (10 分钟)，已自动终止`

---

### 场景 E：状态查询

**步骤 1：任务运行中发送**
```
企微: 状态
```

**预期行为**：
- 如果有任务在跑：`📊 当前正在执行「{任务名}」`
- 如果空闲：`📊 当前空闲，随时可以接收新任务`

---

### 场景 F：任务取消

**步骤 1：任务运行中发送取消**
```
企微：取消
```

**预期行为**：
- ✅ 秘书立即回复：`🛑 已终止当前任务`
- ✅ 进程树被杀（包括所有 Claude 子进程）
- ✅ 日志显示：`[WARNING] opus 被用户中断`

---

## 🐛 常见问题排查

### 问题 1：秘书启动但看不到"已启动"消息

**原因**：企微 API 配置错误或 token 获取失败

**排查**：
```bash
# 查看日志中的错误
grep -i "error\|failed" /opt/opus-v6/logs/secretary.log

# 手动测试企微 API
cd /opt/opus-v6
python3 -c "
import asyncio
from user_interface import UserInterface
import json
with open('config.json') as f: cfg = json.load(f)
ui = UserInterface(cfg)
asyncio.run(ui._wecom_send('📞 测试消息'))
"
```

---

### 问题 2：收到消息但没有反馈

**原因**：opus 子进程启动失败或输出无法解析

**排查**：
```bash
# 1. 检查 opus.py 是否存在且可执行
ls -la /opt/opus-v6/opus.py

# 2. 手动运行 opus 命令
python3 /opt/opus-v6/opus.py --chat "测试" --wecom

# 3. 查看秘书日志中的具体错误
grep "启动 opus\|任务\|失败" /opt/opus-v6/logs/secretary.log
```

---

### 问题 3：任务卡住或无限等待

**原因**：opus 进程卡在确认点等待用户回复（这是正常的）

**解决**：
```bash
# 发送 Redis 消息来响应确认
redis-cli -p 6379 set "opus:reply:default" "1"

# 或在企微回复确认/取消/修改
```

---

### 问题 4：Redis 连接失败

**原因**：Redis 未启动或端口被占用

**排查**：
```bash
# 检查 Redis 是否运行
redis-cli ping
# 期望输出：PONG

# 检查 Redis 监听的端口
netstat -tlnp | grep redis

# 启动 Redis（如未启动）
redis-server &
```

---

### 问题 5：秘书进程内存泄漏

**原因**：长期运行中消息处理异常积累

**解决**：
```bash
# 定期重启秘书
systemctl --user restart opus-secretary

# 查看当前内存占用
ps aux | grep secretary.py
```

---

## 📊 日志分析模板

### 正常的完整流程日志

```
2026-02-15 14:30:12 [secretary] INFO: 秘书进程启动，日志文件: /opt/opus-v6/logs/secretary.log
2026-02-15 14:30:13 [user_interface] INFO: 已获取企微 access_token，有效期至 2026-02-15 14:40:13
2026-02-15 14:30:13 [user_interface] INFO: 秘书进程已启动，随时可以发消息
2026-02-15 14:31:00 [secretary] INFO: 收到消息: 添加登录功能
2026-02-15 14:31:01 [secretary] INFO: 启动 opus: python3 /opt/opus-v6/opus.py --wecom...
2026-02-15 14:31:02 [user_interface] INFO: 收到！正在启动任务...
2026-02-15 14:31:15 [secretary] INFO: opus 任务成功完成，摘要长度: 280
2026-02-15 14:31:16 [user_interface] INFO: 任务「添加登录功能」已完成
```

### 异常流程日志

```
2026-02-15 14:35:00 [secretary] ERROR: 启动 opus 失败: [Errno 2] No such file or directory
# → 检查 opus.py 路径

2026-02-15 14:36:00 [secretary] WARNING: opus 超时 (600s)，强制终止
# → 任务太复杂，超过 10 分钟限制

2026-02-15 14:37:00 [secretary] ERROR: 处理消息失败: ConnectionError
# → Redis 连接失败
```

---

## 🚀 性能调优

### 增加任务超时

编辑 `secretary.py`：
```python
OPUS_TIMEOUT = 1800  # 改为 30 分钟（原为 600s）
```

### 增加提醒频率

编辑 `user_interface.py` 中的 `_wecom_wait_web_reply`：
```python
remind_schedule = [60, 180, 300]  # 改为 1/3/5 分钟
```

### 并发处理多任务

**当前限制**：秘书只能同时处理一个任务（互斥）

**解决方案**：修改 `_handle_message` 移除互斥检查（需谨慎，可能导致资源耗尽）

---

## ✅ 完整验证清单

- [ ] 秘书进程启动时发送欢迎消息
- [ ] 接收简单问答并在 30 秒内返回答案
- [ ] 接收开发任务并正常启动 opus
- [ ] 任务互斥：拒绝并发任务
- [ ] 任务超时：10 分钟后自动终止
- [ ] 状态查询：正确报告当前状态
- [ ] 任务取消：快速杀进程并反馈
- [ ] 日志记录：所有操作都有日志
- [ ] 错误处理：异常不导致秘书崩溃
- [ ] 企微 API：token 自动刷新，不重复获取

---

## 📞 获取支持

如遇问题，收集以下信息：
```bash
# 1. 秘书进程日志（最后 100 行）
tail -100 /opt/opus-v6/logs/secretary.log > secretary_log.txt

# 2. 系统信息
uname -a > system_info.txt

# 3. Redis 状态
redis-cli info > redis_info.txt

# 4. 进程树
pstree -p > process_tree.txt
```

然后提供这些文件以帮助诊断。
