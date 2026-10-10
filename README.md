# ZUsage — 多 Agent Token 用量监控

macOS 菜单栏仪表盘 + 终端命令，实时监控本机各 AI Agent 的 token 消耗与生成速度。
纯本地：只读各 Agent 自己的本地数据库/日志，无网络请求、无第三方依赖。

## 快速开始

前提：macOS 12+，装好 Xcode 命令行工具（`xcode-select --install`，提供 swiftc 与 python3）。

```bash
git clone https://github.com/wsj414855138-code/zusage.git
cd zusage
bash install.sh
```

装完菜单栏出现 ⚡ 图标，点击弹出面板。装在哪个目录都行（脚本会把采集器部署到
`~/.zcode/cli/agent-usage/` 作为运行时副本，见下文「部署模型」）。

- 首次添加登录项时 macOS 会弹一次「自动化」授权弹窗，允许即可（拒绝也不影响使用，只是开机不自启）。
- `zusage` 命令装在 `~/.local/bin/`，若提示找不到命令，把它加进 PATH：
  `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc`
- 面板里只显示**你本机实际在用的** Agent（比如没用 Pi 就不会有 Pi 这一行）。
- 卸载：退出菜单栏 App 后，删除 `~/Applications/ZUsage.app`、
  `~/Library/LaunchAgents/local.zusage.agentusage.plist` 与 `~/.zcode/cli/agent-usage/` 即可，无其他残留。

## 隐私

采集器只**读取**本机 Agent 的日志/数据库（路径见下表），聚合结果写入本地的
`~/.zcode/cli/agent-usage/usage.db`。全程无网络请求、无遥测、无任何数据上传；
读 Agent 库一律只读优先，不写对方任何数据。

## 监控对象

| Agent | 数据源 | 粒度 | 速度 |
|---|---|---|---|
| ZCode | `~/.zcode/cli/db/db.sqlite` 的 `model_usage` 表 | 逐请求 | ✅ tok/s（输出 ÷ 首字到完成的耗时） |
| Codex | `~/.codex/sessions/**/*.jsonl`（`turn_context` 给模型，`token_usage_record` 给用量） | 逐请求 | ✅ tok/s（墙钟窗口法，见下） |
| WorkBuddy | `~/.workbuddy/projects/**/*.jsonl` transcripts（模型按 sessionId 关联 `workbuddy.db` 的 `sessions` 表） | 逐请求 | ✅ tok/s（墙钟窗口法） |
| Pi | `~/.pi/agent/sessions/**/*.jsonl` 的 assistant 消息 | 逐消息 | ✅ tok/s（墙钟窗口法；带美元成本，UI 已隐藏） |

新增 Agent：在 `sync/sync.py` 里照抄一个 `sync_*` 适配器，并在 `main()` 的列表里注册。

## 架构

```
 各 Agent 本地数据            统一库                  展示
 ┌──────────────┐  60s    ┌─────────────────┐   ┌──────────────────┐
 │ zcode sqlite │──┐      │ ~/.zcode/cli/   │   │ 菜单栏 Popover    │
 │ codex jsonl  │──┼─sync─│ agent-usage/    │←──│ (Swift, 5s 刷新,  │
 │ workbuddy db │──┤      │ usage.db (WAL)  │   │  数据>45s 补同步)  │
 │ pi jsonl     │──┘      └─────────────────┘   └──────────────────┘
                            ▲
                    launchd (launchd/*.plist)
                    另: zusage CLI 直接读库
```

- **幂等**：所有行按 `(agent, key)` upsert，重复跑不重复计数。
- **统一口径**：`input_tokens` 一律是「不含缓存」的输入，缓存单独进 `cache_read_tokens`。
  ZCode / Pi 的来源本身如此；Codex 的 `input_tokens` 含 `cached_input_tokens`，适配器已相减，
  否则 `input + cache` 求和会对 Codex 双计。
- **单位区分**：`unit` 列标明口径 —— 当前四家均为 `token`；`quota`（会话配额消耗）为
  WorkBuddy 旧采集口径，已在 v2 迁移中清空，列本身保留以备将来接入配额型数据源。
- **实时性**：ZCode 由面板直读其 sqlite（实时）；其余 Agent 靠 launchd 60 秒周期，面板检测到
  数据超过 45 秒未更新时会在后台补跑一次同步（主线程不阻塞）。
- **故障安全**：读库全部优先只读 + busy_timeout；同步失败时面板保留上次数值（不闪空）；
  单个数据源异常不影响其他源。
- **部署模型**：`sync/sync.py` 是源文件，`install.sh` 会把它复制成运行时副本
  `~/.zcode/cli/agent-usage/sync.py`，launchd 与 App 都指向副本。
  原因是 **macOS TCC 保护 `~/Desktop`**：launchd 拉起的进程直接读桌面下的脚本会报
  `Operation not permitted`（实测退出码 2）。所以**改完采集器源码必须重跑
  `bash install.sh` 重新部署**，否则定时同步还在跑旧副本。

## 文件地图（review 入口）

| 文件 | 职责 |
|---|---|
| `sync/sync.py` | 采集器**源文件**：4 个适配器 + 统一库 schema + 增量/版本号/容错逻辑 |
| `menubar/main.swift` | 菜单栏 App：NSStatusItem + NSPopover 仪表盘、数据聚合、标题模式 |
| `cli/zusage` | 终端报表：多 Agent 汇总表 + 按 Agent·模型的 24h 速度 |
| `tests/smoke_test.py` | 适配器冒烟测试（离线 fixtures，27 项断言，不依赖真实 Agent 数据） |
| `install.sh` | 一键安装/更新：按实际路径生成 plist、写 App 配置、装登录项 |
| `menubar/build.sh` | swiftc 构建 → `~/Applications/ZUsage.app` |
| `menubar/render-icon.swift` | 开发工具：渲染图标预览 PNG |

## 安装 / 更新

```bash
bash install.sh
```

脚本依次做五件事：构建 App → 安装 `zusage` CLI → **把 `sync/sync.py` 部署为运行时副本
`~/.zcode/cli/agent-usage/sync.py`** → 按实际路径生成 launchd plist 并将路径写入 App 的
`syncScriptPath` → 加登录项并跑一次同步。项目目录可放在任意位置（含桌面）。

> **改了采集器源码后要重跑 `bash install.sh`**：launchd 跑的是运行时副本，不是源文件。
> 只改菜单栏 App（`menubar/main.swift`）或 CLI 则不需要——前者由 `build.sh` 直接重编，
> 后者通过符号链接指向项目里的文件。

## 测试

```bash
python3 tests/smoke_test.py
```

26 项断言，覆盖：幂等性、Codex 模型按 `turn_id` 关联、Codex input 与 cache 不双计、
null 时间戳不致命、读取失败的文件不记签名（下轮重试）、WAL 库缺副文件时退回读写连接、
WorkBuddy 逐请求口径（input 扣缓存 / 墙钟窗口 gen / sessionId 关联模型 / 旧 quota 行迁移清理）、
统一库为 WAL、提取逻辑版本号变化触发历史重读。

## 使用

- 菜单栏 ⚡ 图标：标题显示 `速度 · 今日输出`（面板底部可切「仅速度」）。点击弹出面板：
  头条大数字、今日四格（悬停显示精确值）、Agent 拆分（含每 Agent 前 3 个模型 ↳）、
  模型速度排行、近 7 天柱状图（悬停显示当日真实值）。
- 面板底部「视图：用量 / 视图：速度」按钮可切换 Agent 区的口径：
  - **用量**（默认）：Agent 与模型显示今日 / 近 7 天 token 量，条形按周用量占比。
  - **速度**：Agent 与模型显示 24h 生成速度（tok/s）与样本数，条形按速度占比，Agent 按速度排序；
    24h 无生成样本的 Agent 归入一行提示；下方的「模型速度」整块会折叠（避免与 Agent 区重复）。
  ZCode 有真实的逐请求耗时（`first_token_at` / `completed_at`）；Codex、Pi、WorkBuddy 的
  会话日志没有耗时字段，但每行带 `timestamp`，用**墙钟窗口法**还原：一次响应的生成时长 ≈
  该响应落盘时刻 − 上一条工具结果的落盘时刻（Codex 以 `event_msg/task_started` 与
  `response_item/custom_tool_call_output` 为起点，Pi 以 `toolResult` 消息为起点，
  WorkBuddy 以 `function_call_result` 记录为起点），过滤 0.3s–10min 之外的异常窗口。
  该口径含首字延迟与调度间隙，系统性略偏慢，但跨 Agent 可比。四个 Agent 均已接入，
  速度视图不再有「无速度数据」行。切换结果会记住（UserDefaults `speedView`）。
  **颗粒保留窗口**：面板所有查询都限定最近 7 天（本地零点 − 6 天起）——任何 Agent 或
  模型颗粒 7 天无请求即自然从面板消失（历史颗粒留在库里，不展示），速度视图再叠加
  24h 样本过滤；ZCode 作为头条固定显示。
- 终端：`zusage`、`zusage --today`、`zusage --days 30`、`zusage --sync`。
- 配置项（`defaults write local.zusage.zusage <键> <值>`）：
  - `titleMode` — 0 = 速度+输出，2 = 仅速度
  - `speedView` — true = 打开面板即用速度视图，false = 用量视图
  - `syncScriptPath` — 兜底同步脚本路径（install.sh 会自动写入）
  - `autoReposition` — 默认 false。开启后每 60 秒重建状态项，以便菜单栏腾出位置时自动补位，
    代价是图标可能在菜单栏中重新排序（装了 Ice 等管理器时尤其明显）。
  - `debugAutoOpen` — 默认 false。开启后启动即自动弹出面板且**不因失焦关闭**，用于截图验证或演示：
    `defaults write local.zusage.zusage debugAutoOpen -bool true`。
    注意：**改 UserDefaults 要先杀掉 App 再写**，否则 App 退出时 cfprefsd 会把内存里的旧值刷回磁盘、
    覆盖掉外部写入。

## 数据目录

运行时数据（与代码分离）：`~/.zcode/cli/agent-usage/`
- `usage.db` — 统一库（WAL 模式，故可能同时存在 `usage.db-wal` / `usage.db-shm`；备份时一起拷）
- `sync.py` — 采集器运行时副本（由 `install.sh` 从项目部署，launchd 与 App 都用它）
- `sync.err` — launchd 同步的错误输出（空 = 正常）

表结构：
- `requests(agent, key, ts, model, status, input_tokens, output_tokens, cache_read_tokens, gen_ms, cost_usd, unit)`
- `sync_state(adapter, state)` — 增量签名 / 提取逻辑版本号 / `_meta.last_sync`

采集窗口：首次运行回填 **30 天**，之后按 `started_at` 增量（ZCode）或文件签名增量（Codex / Pi）。
尾部窗口重扫 3 小时，用于兼容请求状态从 `running` 变为 `completed`。

## 已知设计取舍

- WorkBuddy 只有会话级配额（`used`/`size`），无法拆到逐请求、也无速度；但每会话带模型。
- Codex 极少数记录拿不到模型（对应 `turn_context` 不在同一文件内），会显示为空。
- ZCode 若超过 30 天未运行，更早的历史不会入库。
- 修改某个源的解析逻辑时，务必把该源的 `*_LOGIC_VERSION` 常量加 1，否则历史文件因
  `mtime:size` 签名未变而不会被重读，修正无法回溯。
- 读活跃 sqlite 一律不用 `immutable=1`（WorkBuddy 用 WAL，immutable 会漏新数据）；
  且只读打开在 WAL 库缺少 `-wal`/`-shm` 副文件时会失败（SQLite 行为，实测连 /tmp 副本也一样），
  采集器会自动退回读写连接 —— 只执行 SELECT，不写任何数据。
- 本机 ZCode 的 `db.sqlite` 是 `delete` 日志模式，故能稳定只读打开。
- 隐私：codex/pi 适配器读取的是含对话内容的会话文件，但只提取用量字段，不落盘任何内容。
- 应用未签名（本地构建、无 quarantine，Gatekeeper 不拦）；换机分发需签名公证。
