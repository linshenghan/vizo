# Vizo 秘书进程修复 - 快速启动指南

> 说明：当前 `Vizo` 主线默认入口已切换为 `vizo`，Docker 主路径请优先参考 `docker-compose.vizo.yml`。本文保留的 `opus-secretary`、`/opt/opus-v6` 等命令示例属于历史兼容排障路径，尚未在本轮统一改造范围内移除。

## 🚀 立即启动（3步）

### Step 1: 重启秘书进程
```bash
systemctl --user restart opus-secretary

# 验证启动
systemctl --user status opus-secretary
# 期望输出：● opus-secretary.service - ... running ...
```

### Step 2: 监看日志（新开一个终端）
```bash
tail -f /opt/opus-v6/logs/secretary.log
```

### Step 3: 在企微发送测试消息
```
发送：Python 是什么
期望回复（30秒内）：
  ✅ 任务「Python 是什么」已完成
  
  Python 是一种高级编程语言...
```

---

## ✅ 修复验证清单

### 问题 1：秘书收到消息后给不出结果 ❌

**修复方式**：添加成功任务反馈

```diff
  if returncode == 0:
-     logger.info("opus 子进程正常完成")  # ❌ 只记录日志
+     # ✅ 成功完成 - 提取结果摘要
+     result_summary = self._extract_result_summary(stdout_text)
+     await self.ui._wecom_send(
+         f"✅ 任务「{self._current_task_desc}」已完成\n\n{result_summary}"
+     )
```

**验证**：发送任何简单问题，应在 30 秒内收到答案

---

### 问题 2：subprocess 输出被忽略 ❌

**修复方式**：添加结果摘要提取

```python
def _extract_result_summary(self, output: str) -> str:
    """从 opus 输出中提取结果摘要"""
    # ✅ 现在处理 stdout 和 stderr
    # 查找关键结果部分或最后几行输出
    # 返回最多 500 字的摘要
```

**验证**：任务完成消息应包含实际的任务输出，而不是空白

---

### 问题 3：异步任务异常被吞掉 ❌

**修复方式**：添加 Task 异常处理

```python
# 在 _handle_message 中
def handle_task_exception(task):
    if not task.cancelled():
        try:
            task.result()  # ✅ 触发异常
        except Exception as e:
            logger.error(f"opus 任务异常: {e}", exc_info=True)

self._opus_task.add_done_callback(handle_task_exception)
```

**验证**：如果 opus 子进程异常，应看到日志错误和企微错误消息

---

## 🧪 快速测试

### 方式 A：命令行测试脚本
```bash
cd /opt/opus-v6
python3 test_secretary.py

# 输出示例：
# [测试 1] 简单问答
#   ✅ 秘书已接收并开始处理
# [测试 2] 状态查询
#   ✅ 秘书已处理状态查询
```

### 方式 B：企微实际测试

```
测试消息 → 秘书反馈对比

修复前：
  发送：Python是什么
  回复：💬 收到，查询中...
  结果：❌ 看不到答案

修复后：
  发送：Python是什么
  回复：💬 收到，查询中...
        ✅ 任务「Python是什么」已完成
        Python 是一种高级编程语言...
```

---

## 📋 修复检查清单

| 检查项 | 命令 | 预期结果 |
|--------|------|--------|
| 秘书启动 | `systemctl --user status opus-secretary` | running |
| 日志存在 | `ls -la /opt/opus-v6/logs/secretary.log` | 文件存在 |
| Redis 连接 | `redis-cli ping` | PONG |
| opus.py 存在 | `ls -la /opt/opus-v6/opus.py` | 文件存在 |

---

## 🔍 故障排查

### 问题 A：秘书启动失败

```bash
# 检查日志
tail -50 /opt/opus-v6/logs/secretary.log

# 重启 Redis
redis-server &

# 重启秘书
systemctl --user restart opus-secretary
```

### 问题 B：消息收不到成功反馈

```bash
# 检查秘书是否运行
ps aux | grep secretary.py

# 查看日志中是否有 "启动 opus"
grep "启动 opus" /opt/opus-v6/logs/secretary.log

# 手动测试 opus
python3 /opt/opus-v6/opus.py --chat --wecom "test"
```

### 问题 C：企微 API 错误

```bash
# 检查配置
cat /opt/opus-v6/config.json | grep -A 5 '"wecom"'

# 检查日志中的 API 错误
grep -i "error\|failed\|errcode" /opt/opus-v6/logs/secretary.log
```

---

## 📊 性能指标

修复后秘书进程的典型响应时间：

| 任务类型 | 响应时间 | 包含内容 |
|--------|--------|--------|
| 简单问答 | 20-30s | ✅ 完整答案 |
| 开发任务 | 5-60min | ✅ 多个确认 + 结果 |
| 状态查询 | < 2s | ✅ 当前状态 |
| 任务取消 | < 1s | ✅ 确认消息 |

---

## 🎯 修复验证流程

### 验证步骤 1：检查代码修改
```bash
# 检查是否包含成功反馈代码
grep -n "✅ 任务" /opt/opus-v6/secretary.py
# 应该找到在 _run_opus 方法中的代码

# 检查是否包含结果摘要提取
grep -n "_extract_result_summary" /opt/opus-v6/secretary.py
# 应该找到方法定义
```

### 验证步骤 2：重启秘书
```bash
systemctl --user restart opus-secretary
sleep 2
systemctl --user status opus-secretary
```

### 验证步骤 3：实际测试
```
1. 在企微发送：Python 是什么
2. 预期立即收到：💬 收到，查询中...
3. 预期 30 秒内收到：✅ 任务「Python 是什么」已完成 + 答案
```

### 验证步骤 4：查看日志
```bash
tail -20 /opt/opus-v6/logs/secretary.log | grep -E "(INFO|ERROR)"
# 应该看到：
# [INFO] 收到消息: Python 是什么
# [INFO] 启动 opus: ...
# [INFO] opus 任务成功完成，摘要长度: 450
```

---

## 💡 常见问题

**Q: 修复后秘书仍然看不到结果?**
A: 检查 opus.py 是否可以正常运行。运行 `python3 /opt/opus-v6/opus.py --chat --wecom "test"` 手动测试。

**Q: 日志中有很多错误怎么办?**
A: 查看日志具体内容: `tail -100 /opt/opus-v6/logs/secretary.log`。常见原因：
- Redis 连接失败
- 企微 API 配置错误
- opus 子进程启动失败

**Q: 秘书进程内存持续增长?**
A: 定期重启秘书: `systemctl --user restart opus-secretary`

---

## 📞 获取帮助

如问题仍未解决，收集以下信息：

```bash
# 1. 秘书日志
tail -100 /opt/opus-v6/logs/secretary.log > secretary.log.txt

# 2. 配置文件（隐去敏感信息）
cat /opt/opus-v6/config.json > config_sanitized.json

# 3. 系统信息
uname -a > sysinfo.txt
python3 --version >> sysinfo.txt
redis-cli ping >> sysinfo.txt

# 4. 进程列表
ps aux | grep -E "secretary|opus|redis" > processes.txt
```

然后提供这些文件以帮助诊断问题。
