# 智能体运行与批测

仓库根目录同时包含 UAV 技能、计算运行时、数据库、当前使用的 nanobot 源码，以及单条和批量测试入口。

## 安装

需要 Python 3.11 或更新版本。从仓库根目录执行：

```bash
python -m venv nanobot-source/.venv
nanobot-source/.venv/bin/python -m pip install -e ./nanobot-source
nanobot-source/.venv/bin/python uav_runtime/setup.py
```

`nanobot-source` 是本次实际安装使用版本的源码快照，不是工作区内旧的同名副本。保留上游 MIT 许可、依赖清单和来源清单 `nanobot-source/SOURCE_MANIFEST.json`。

## 配置

```bash
export UAV_MODEL='Qwen3.8-27B'
export UAV_PROVIDER='vllm'
export UAV_API_BASE='http://127.0.0.1:7696/v1'
# 需要鉴权时设置 UAV_API_KEY；本地无鉴权服务默认使用 EMPTY。
nanobot-source/.venv/bin/python runtime-config/prepare_config.py
```

生成的 `runtime-config/local.json` 默认仅当前用户可读写，已被 Git 忽略；脚本拒绝覆盖已有配置，也不修改用户目录下的 nanobot 配置。模型端点可由环境变量覆盖，不上传私人 API Key。

示例配置同步了当前运行设置：64,000 token 上下文、8,192 输出上限、200 次工具迭代、10,000 字符工具回传、`presencePenalty=0.5`；关闭 `list_dir`、`glob`、`grep`、网络、消息等对应工具，保留现行 `exec` 和子任务工具配置。临时目录使用系统默认位置，避免依赖原机器的绝对路径。

`AGENTS.md` 当前为 `PLANNING_ONLY` 提示模式。部署在线飞控时，需要另外部署中央服务并核对其配置；运行配置不会凭空提供 ROS 或中央服务。

## 单条与批量测试

```bash
nanobot-source/.venv/bin/python chat_with_bot/chat_single.py
nanobot-source/.venv/bin/python chat_with_bot/chat_multi.py
```

单条示例位于 `chat_single.py` 末尾；批量输入读取 `chat_with_bot/queries.txt`，并发数在 `chat_multi.py` 设置。两者自动使用仓库内的 nanobot 快照；可用 `NANOBOT_SOURCE`、`UAV_WORKSPACE` 和 `NANOBOT_CONFIG` 指向其他安装、工作区或配置。运行上述命令会调用配置的模型。

输出仍区分正文和推理，API 记录、轨迹与结果 JSONL 写入 `chat_with_bot/`，均被 Git 忽略。批量任务使用配置锁，构造模型后恢复配置，单条失败不终止整批。

生成预算实测脚本为 `chat_with_bot/check_generation_budget.py`，需要显式 `--live`；默认不限制额外推理预算。提取与诊断工具见 `extract_llm_calls.py`、`analyze_reasoning_duplication.py` 和已有的 `check_reply_json.py`。

## 无模型调用的验证

```bash
nanobot-source/.venv/bin/python -m pip install pytest pytest-asyncio
PYTHONPATH=nanobot-source nanobot-source/.venv/bin/python -m pytest \
  nanobot-source/tests/agent/test_runner.py \
  nanobot-source/tests/providers/test_generation_budget_logging.py
PYTHONPATH=nanobot-source:chat_with_bot nanobot-source/.venv/bin/python -m unittest \
  discover -s chat_with_bot -p 'test_*.py'
uav_runtime/.venv/bin/python -m unittest discover -s tests
```

这些检查使用假模型或本地计算；不会下发无人机任务。
