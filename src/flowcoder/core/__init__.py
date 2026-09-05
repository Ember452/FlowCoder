"""核心基础设施：cache、cron、driver、frontmatter、serialization。

第 0 层共享原语：不得 import 任何其他 flowcoder 模块。cron（5 字段
解析与 next_after）自 scheduler/ 下沉至此，供 scheduler 与 config
校验共同使用。

各模块经子模块直接引用（flowcoder.core.cache 等），本包仅确立
常规包边界（与全项目子包约定一致）。
"""
