# ADR：沙箱接入工具链与权限门（P1c）

- 状态：Accepted（真实容器三场景演示延后，见文末）
- 日期：2026-08-29
- 关联：PROMPTS.md P1c、docs/specs/2026-08-29-sandbox-p1a-adr.md、
  docs/specs/2026-08-29-sandbox-p1b-adr.md

## 背景

P1a/P1b 建成沙箱执行与容器池后，本阶段把 Bash 工具的执行从裸 subprocess 切换到
沙箱：`sandbox_mode: off | docker`（默认 off），TUI 斜杠命令 `/sandbox` 会话内
切换并持久化到用户配置。约束：off 路径零改动、全部既有测试通过、权限审批语义
不变。

## 决策与理由

### D1：默认 off

- **行为兼容是验收标准**：off 走原 subprocess 路径，代码未动一行，既有用户
  升级后零感知。
- Docker 是可选依赖（`flowcoder[sandbox]`），默认路径不应背上"未装 Docker 就
  报错"的新故障面。
- 沙箱改变文件可见性语义（容器内只有挂载的白名单目录可见），部分工作流
  （依赖本机全局工具链、访问工作目录之外的文件）在 docker 模式下行为不同。
  这是功能取舍而非缺陷，必须由用户显式选择，不能替用户决定。
- 落点：配置键 `sandbox_mode`（顶层，校验 off/docker），Bash 构造时由
  agent factory 从 AppConfig 注入初始值。

### D2：权限门在沙箱之前，而不是之后

- **结构性保证而非约定**：agent core 的执行序是 `_authorize_tool`（四模式审批、
  危险命令检测、路径沙箱、规则引擎）→ `tool.execute`。沙箱切换实现在
  `Bash.execute` 内部，天然位于授权之后——不依赖任何"记得先审后跑"的纪律。
- 审批语义不变：用户看到的是同一条 Bash 工具调用，无论背后是 subprocess 还是
  容器。审批的是"要不要执行这条命令"，执行通道是实现细节。
- **为什么不能反过来**（先沙箱再审批）：审批门是安全边界的最后决策点，若先
  进沙箱，则 (a) 未审批的命令已消耗容器资源并有部分副作用（文件写入容器层），
  (b) 规则引擎的 deny 决策将发生在沙箱已初始化之后，扩大了不可信代码实际运行
  的面；(c) `bypassPermissions` 等模式在沙箱前置架构下会变得含义模糊。
- 危险命令规则与路径校验逻辑零改动、零绕过：它们全部在授权层，对执行通道
  无感知。

### D3：工作目录映射用读写挂载，不用 tar 传输

P1a 评测场景（不可信代码）默认零挂载；bash 工具场景相反——命令的意义就在于
操作项目文件。两个可行方案：

- tar 传输（P1a transport）：每条命令前把工作目录整体打进出、执行后取回变更
  ——大仓库 IO 不可接受，且命令间文件状态需要双向同步，语义复杂易错。
- **读写 bind mount 白名单工作目录 → 容器 /workspace**（本 ADR 选择）：文件
  状态天然共享、命令语义与 off 模式一致；暴露面被收窄为单一白名单目录
  （agent 的 work_dir），且整个命令已先过权限门（D2）。

`SandboxConfig.mounts` 是显式参数：默认构造的沙箱容器仍然零挂载，评测等
不可信执行场景的安全默认不受影响。

### D4：模式状态放 Bash 实例，切换即生效并持久化

- Bash 是 registry 中的单例，`/sandbox` 经 `agent.registry.get("Bash")` 直接
  改状态，daemon/GUI 会话与 TUI 共享同一 agent 实例时状态天然同步；
  `/status` 增加沙箱行展示当前模式。
- 持久化：`update_user_config_value()` 对 `~/.flowcoder/config.yaml` 做
  **整行替换/追加**而非 YAML 重解析回写——保留用户文件里的注释与排版
  （daemon 既有 save 路径是整体重写，本 helper 不复用其缺陷）。
- 启动时 `create_agent_from_config` 读取 `config.sandbox_mode` 注入，重启后
  配置仍生效。

### D5：Docker 不可用必须显式报错，不得静默失败

两个路径，两种时机，同一个原则：

- **会话内切换**（`/sandbox docker`）：`set_sandbox_mode` 先做可用性预检
  （预热池即预检——Docker SDK 未装 / daemon 不可达在 `start()` 立即暴露），
  失败则保持 off 并返回带原因的错误信息（含安装指引），不切换、不持久化。
- **配置声明 docker 但启动时不可用**：启动不阻塞（配置是声明意图，不让
  可选依赖拖死主流程），模式照常置为 docker；首次执行时容器池惰性创建失败，
  返回 `Error executing in sandbox: <原因>` 的明确错误结果。两条路都把原因
  摆在用户面前。

### D6：bash 工具专用池规模为 2

交互式会话串行执行为主，2 个预热容器足以覆盖偶发并发；每容器 256MB 限额，
常驻内存 ~512MB。评测场景（P2a）按需自建池，不共用此实例。

## 后果

- off 模式零行为变化（全部既有测试通过即为证）；docker 模式的接入逻辑由
  fake pool 单元测试覆盖（输出格式、退出码语义映射、超时、SandboxError、
  切换失败回退、持久化调用）。
- 真实容器三场景（超时脚本被杀、内存超限被限、断网命令失败）挂 docker marker
  待环境就绪补录。
- daemon 侧未新增独立的状态同步 payload：状态源就是共享 Bash 实例，
  `/status`（TUI 与 daemon 命令通道均可触发）即真实状态源；如 GUI 需要
  推送式同步，在 daemon 事件流上加一条状态事件即可，属增量工作。

## 真实容器验收回填（待 Docker 环境就绪）

- [ ] `/sandbox docker` 切换成功，`/status` 显示沙箱: docker
- [ ] docker 模式下 `echo` / `python` 命令正常执行，工作目录文件可见可写
- [ ] 超时脚本被杀（`sleep 999`，timed_out 报错）
- [ ] 内存超限被限（`python -c "bytearray(2**30)"` 非零退出）
- [ ] 断网命令失败（容器内无网络接口）
- [ ] 权限门仍在沙箱之前：dangerous command 在 docker 模式下先被审批层拦截
