# 安全策略

## 支持版本

| 版本 | 是否提供安全修复 |
|---|---|
| 最新 release | ✅ |
| 旧版本 | ❌（请升级到最新版） |

## 报告漏洞

**请不要在公开 Issue 中描述安全漏洞。**

1. 通过 GitHub 的 "Private vulnerability reporting"（仓库 Security 标签页 → Report a vulnerability）私下报告
2. 报告请包含：受影响模块、复现步骤/概念验证、影响评估
3. 收到后 72 小时内确认，修复前与你同步时间线

## 已知安全设计边界

本项目在安全上做了以下承诺，报告漏洞时可对照：

- **路径沙箱**：文件工具的读写必须落在白名单目录 resolve 之后（含 symlink、`..`、Windows 盘符大小写等非常规路径）
- **权限门**：工具执行必须先过四模式审批，沙箱是权限门的下游而非替代
- **执行隔离**：`sandbox_mode=docker` 时命令在容器内以 non-root、断网、资源受限方式执行

历史安全修复记录见 `docs/specs/` 下的 ADR 与 `CODE_QUALITY_AUDIT.md`。
