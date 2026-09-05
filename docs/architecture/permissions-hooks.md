# 权限与钩子

权限四模式审批、规则文件；钩子引擎：conditions/actions/executors。

## 安全命令白名单（免审批只读命令）

命令类工具（Bash 等）在进入规则引擎/审批流之前，先经 `permissions/dangerous.py::is_safe_command` 判定是否为免审批的只读命令。判定规则（2026-09 收紧后）：

1. 不含管道/重定向/命令替换等复合符（`|`、`;`、`&&`、`>`、`` ` `` 等）
2. **不含 `$`**——命令经 shell 执行，变量会在运行期展开（如 `echo $API_KEY`），含 `$` 一律转人工审批
3. 命令在白名单内（`pwd`、`git status`、`ls` 等；`env`/`printenv` 已移除）
4. 读文件类命令（`cat`/`head`/`tail`/`grep`/`diff` 等）的参数**不得指向凭据类路径**（`.flowcoder`/`.ssh`/`.aws`/`.kube`/`.netrc`/`id_rsa`/`.env` 等，大小写不敏感），否则转人工审批——防止 Agent 免审批把密钥读进对话上下文送给 LLM 供应商

设计动机与完整清单见 `docs/plans/QUALITY_OPTIMIZATION_PLAN.md`。

