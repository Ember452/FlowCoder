# LLM 客户端

多供应商适配：Anthropic Messages、OpenAI Compat/Responses，统一错误映射与上下文窗口管理。

## 结构

| 模块 | 职责 |
|---|---|
| `core.py` | `LLMClient` 抽象 + 三协议实现（Anthropic/OpenAI Responses/OpenAI Compat）+ `create_client` 工厂 |
| `errors.py` | 统一错误体系：`LLMError` 基类，`AuthenticationError` / `RateLimitError`（带 retry_after）/ `NetworkError` / `ServerError`（5xx）/ `LLMTimeoutError` |
| `error_mapping.py` | 各 SDK 原生异常 → 统一错误（5xx 单独映射为可重试的 `ServerError`） |
| `resilience.py` | 韧性层（P3）：`ResilientClient` 装饰器（重试/限流/超时）+ `TokenBucket` + 退避计算 |

## 韧性层（P3）

`create_client` 工厂统一把协议客户端包进 `ResilientClient`，消费方零感知：

- 重试：429 / 5xx / 网络错误 / 超时按指数退避（带抖动）重试，尊重 retry-after；
  鉴权失败与 4xx 立即失败。**流式边界：仅"零事件交付"的失败可整体重试**，
  流中途失败无法重放、原样抛出。
- 限流：进程内异步令牌桶（`rate_limit_rpm`），并发请求平滑排队。
- 超时：`request_timeout_s` 兜底"无事件产出"的挂死（事件持续产出则续期）。
- 配置：provider 级 `max_retries`（默认 2）/ `rate_limit_rpm` / `request_timeout_s`。

## 温度（P2b）

`ProviderConfig.temperature`（None=provider 默认）透传三协议；
thinking 模式与 temperature 互斥（Anthropic API 约束），开启 thinking 时不传。
