# 配置系统

YAML 配置加载、多层合并、validator 校验、用户配置持久化。

## 配置文件与层级

按优先级从低到高合并（`load_config`）：

1. `~/.flowcoder/config.yaml`（用户级）
2. `<cwd>/.flowcoder/config.yaml`（项目级）
3. `<cwd>/.flowcoder/config.local.yaml`（本地覆盖，不入 git）

同键后者覆盖前者（provider 列表整体替换，mcp_servers 按名字合并）。
账号登录时会注入云端合成的 gateway providers（不落盘）。

## AppConfig 顶层键

| 键 | 类型/默认 | 说明 |
|---|---|---|
| `providers` | 必填列表 | 模型供应商（见下） |
| `schema_version` | int = 1 | 配置 schema 版本 |
| `permission_mode` | str = "default" | 权限四模式初始值（default/acceptEdits/plan/bypassPermissions/custom/dontAsk） |
| `sandbox_mode` | "off" \| "docker" | **P1c**：Bash 工具沙箱执行通道，默认 off（原 subprocess 路径零改动） |
| `mcp_servers` | 列表 | MCP server 配置（command 或 url 二选一） |
| `hooks` | 列表 | 生命周期钩子 |
| `memory` | 对象 | 记忆 provider 配置 |
| `enable_fork` / `enable_verification_agent` / `enable_coordinator_mode` | bool | 能力开关 |
| `worktree` | 对象 | worktree 隔离配置（symlink 目录、过期清理） |
| `teammate_mode` | "" \| "in-process" | teammate 执行后端 |

## ProviderConfig（providers 列表项）

| 键 | 默认 | 说明 |
|---|---|---|
| `name` / `protocol` / `base_url` / `model` | 必填 | protocol ∈ anthropic / openai / openai-compat |
| `api_key` | "" | 支持 `${ENV_VAR}` 展开；空则回退 ANTHROPIC_API_KEY / OPENAI_API_KEY |
| `thinking` | false | 思考模式（与 temperature 互斥，Anthropic API 约束） |
| `temperature` | None（provider 默认） | **P2b**：采样温度，校验 [0,2]；评测默认固定 0.0 保证可复现 |
| `max_retries` | 2 | **P3**：429/5xx/网络错误/超时的重试次数（韧性层） |
| `rate_limit_rpm` | None | **P3**：进程内令牌桶限流（请求/分钟） |
| `request_timeout_s` | None | **P3**：单请求"无事件产出"超时兜底 |
| `context_window` / `max_output_tokens` | 0（自动解析） | 0 = 走四层回退链（配置 → 自动拉取 → 模型映射表 → 保守默认） |

## 校验与合并语义

- `validator.py` 是唯一入口（`validate_config_structure`）：逐键校验 + 清洗，
  未知结构拒绝、移除能力（removed capabilities）直接报错。
- `AppConfig` 的可选键带 `*_declared` 标记：多层合并时只有"显式声明过"的键
  才覆盖低层——未声明的键保持低层值，避免默认值意外覆盖用户配置。

## 运行时持久化（非静态配置）

- `update_user_config_value(key, value)`：把顶层标量键写入用户级
  config.yaml——**整行替换/追加**而非 YAML 重解析回写，保留用户文件的
  注释与排版。`/sandbox` 命令持久化 sandbox_mode 走这条路径。
- daemon 的配置读写路由（`/api/config`）整体重写 config.yaml
  （既有行为，会丢注释）。

## 相关文档

- sandbox_mode 语义：docs/architecture/sandbox.md、P1c ADR
- temperature 与韧性参数：docs/architecture/llm-client.md、P2b/P3 ADR
