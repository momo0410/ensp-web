# eNSP Web 网络设备管理系统

由本程序生成 eNSP 拓扑文件（`.topo`）并注入 eNSP 客户端自动开机，再通过
**本地 Console 端口**（`127.0.0.1:<com_port>`）连接华为路由器/交换机，采集设备信息、
识别设备能力、解析协议角色、以填空式表单下发配置，并内置可自行接入模型服务的 **AI 助教**。

前端为 **React 19 + Mantine 9 + TypeScript**（Vite 构建，源码在 `frontend/`，
单元测试 `cd frontend && npm test`）。
旧版原生 JS 界面保留在 `static/`，可通过 `/legacy` 入口访问——
**旧版已冻结**：不再新增功能，仅修复致命问题，计划在学期末移除。

---

## 项目背景

本系统用于管理 eNSP（Enterprise Network Simulation Platform）中模拟的华为网络设备。

**关键说明：**
- Python 后端**必须运行在安装 eNSP 的 Windows 主机上**
- 设备接入方式是 **eNSP 本地 Console 端口**（`127.0.0.1:<com_port>`），
  设备**不需要管理 IP、不需要配 SSH、不需要网卡同网段**
- 拓扑由 `tools/gen_topology.py` 生成 `.topo`，无需在 eNSP 里手工拖设备/连线
  （也无需配置 eNSP Cloud 桥接）
- 支持 18 种型号，分 7 类：路由器 / 三层交换机 / 交换机 / 防火墙 /
  无线控制器 / 无线接入点 / **终端设备**（PC、Laptop、MCS、STA）
- 设备开机由 `tools/ensp_control.dll` 注入 eNSP 客户端后远程触发
- 拓扑的权威来源是生成的 `.topo` 文件与 `tools/demo_topo/active_links.json`；
  程序不再从设备反推拓扑

---

## 快速开始（五步上手）

> **单实例**：同一工作目录同时只能跑一个后端进程，否则拓扑/设备数据会互相覆盖。
> 重复启动会被拒绝并提示占用者（见 `.instance.owner`）。

> **工作目录**：拓扑、设备表、连线、设备配置都放在一个用户可指定的工作目录下，
> 默认 `workspaces/`，**每个拓扑一个子目录**。首次运行会把旧的 `devices.json`
> 自动迁入默认拓扑，不会丢数据。

1. **建拓扑** → 在"设备管理"页「加载综合园区网案例并开机」（内置 7 设备预设，含两台 PC），
   或「添加设备（自动开机）」逐台添加；程序会生成 `.topo`、注入 eNSP 并自动开机
2. **采集信息** → 「全量采集」，经本地 Console 读取每台设备的 display 输出
3. **能力识别** → 并行探测每台设备支持的协议（三态：支持/已配置/运行中）
4. **协议角色** → 查看每台设备在每个协议里的角色，点"填空配置"直接进预填表单
5. **AI 助教** → 右下角悬浮球，基于设备实测数据答疑、诊断报错、指导实验

---

## 核心机制

### 协议能力发现（三层证据）

eNSP 设备不会"报告"自己支持什么，镜像又只实现了真实 VRP 的子集，
所以系统用三层证据回答"这台设备支持哪些协议"：

| 证据层 | 机制 | 特点 |
|--------|------|------|
| **镜像能力矩阵** | `display version` 解析型号+镜像版本（如 S5700 V200R001C00）查预置矩阵 | 权威、零往返；确定不支持的协议直接不再探测 |
| **? 命令树** | 采集 `display ?` / 系统视图 `?` / 接口视图 `?` 三层关键字与协议签名比对 | 实测证据，零副作用（所有 `?` 单独成行，不会执行命令） |
| **display 探测** | 对每项能力发探测命令，配合多候选命令降级误判 | 判定 configured / running 两态 |

判定优先级：**? 树实测 > 矩阵 > display 探测**，每个能力条目带 `evidence` 字段标明依据。
探测延迟有多层保障：采集缓存优先（可零连接）、矩阵跳过无效探测、候选命令懒执行、
`?` 树采集有硬性时间预算、探测结果缓存 30 分钟（eNSP 镜像的支持集合不会变）。

### 协议角色与填空式配置

`role_detector` 纯解析已有探测输出（零连接），回答"这台设备在每个协议里扮演什么角色"：
VRRP Master/Backup、STP 根桥/根端口、DHCP Server/中继/Snooping、NAT Easy-IP/静态/Server
（含出接口）、ACL 类型与接口引用方向、OSPF 进程/区域/邻居状态等。

在"协议角色"页点 **填空配置**：自动跳到对应模板弹窗，并按设备现状预填可推导的空格
（接口、虚拟 IP `.254`、Router ID、DHCP 网关/网段/掩码、ACL 编号 +1、NAT 出接口沿用现网配置），
学生补齐剩余空格 → 预览 → 确认下发，走既有的备份/验证/保存链路。

### 设备侧连通性验证

管理面数据只能说明"配了没有"，实验验收需要数据面证据：
`POST /api/devices/{ip}/device-ping` 在设备上执行 VRP `ping`（支持 `-a` 源地址）并解析丢包/时延。
AI 实验指导面板内置该工具，结果可一键交给助教分析。

### AI 助教（模型服务自行接入）

- **接入方式**：OpenAI 兼容协议（`/chat/completions`），API 地址、Key、模型名在
  AI 助教"设置"里填写，存于本机 `ai_config.json`（已加入 .gitignore）。
  GLM：`https://open.bigmodel.cn/api/paas/v4`；DeepSeek：`https://api.deepseek.com/v1`；
  本地 Ollama：`http://127.0.0.1:11434/v1`（Key 随意填非空值）。
- **上下文注入**：选择设备后，能力矩阵/协议角色/接口 IP/配置摘要自动进入提示词，
  AI 回答基于这台设备的实测数据，提示词中硬性写明 eNSP 镜像事实、禁止 Cisco 语法。
- **两种模式**：助教模式（引导式教学，默认）/ 工程师模式（直接给命令）。
- **安全红线**：AI 只建议不执行——命令块可"发送到工作台"或"打开填空模板"，
  下发必须走既有的备份 → 预览 → 确认 → 验证链路。
- **无 Key 降级**：未配置时快捷动作（能力解读/角色讲解/解释配置/诊断报错）
  自动降级为本地确定性解读，机房断网也能用。
- **实验指导**：预置 8 个实验（静态路由/OSPF/VLAN Trunk/DHCP/VRRP/NAT/ACL/GRE），
  进度校验基于能力识别缓存（零连接），助教按进度给引导提示，不直接给整段答案。

---

## 功能列表

### 基础功能
1. 由设备表自动生成 eNSP 拓扑文件（`.topo`）并注入 eNSP 自动开机
2. Console 端口连通性探测（`127.0.0.1:<com_port>`）
3. 设备增删改查、并行采集 10+ 条 display 命令
4. JSON 持久化（filelock 并发保护）；能力缓存/实验会话同样落盘，重启不丢
5. 浅色/深色/跟随系统三模式主题

### 能力识别与角色
6. 三层证据的协议能力发现（矩阵 / ? 命令树 / display 探测），31 项协议
7. 设备角色分类（路由器/交换机/三层交换机），镜像家族与置信度展示
8. 协议角色总览页与证据徽标（?树实测 / 镜像矩阵 / 探测）
9. 零副作用 `?` 命令树协议发现接口（本地 Console 通道）

### 配置下发
10. 20+ 单功能模板 + 组合业务方案，按设备角色动态显示
11. 填空式表单（按设备现状预填）+ 配置预览 + 下发后验证 + 可选保存
12. 高级命令模式（危险警告 + 二次确认）、自由配置工作台（`{{变量}}` 模板 + 冲突检测）
13. 一键复制设备配置（自动适配设备身份与冲突 IP）
14. 下发前自动备份，备份文件可回退

### 教学辅助
15. AI 助教（上下文注入 / 流式对话 / 报错诊断 / 命令转工作台）
16. 实验任务指导（8 个实验、自动进度校验、引导式提示）
17. 设备侧 ping 连通性验证

### 安全与运维
18. 下发历史记录（task_history），AI 配置与访问令牌均不入 git
19. 用户设置面板（顶栏「设置」）：控制项目与 eNSP 的联动开关，见下方「项目设置」

---

## 项目设置

顶栏「设置」按钮打开设置面板，控制与 eNSP 的联动行为。标注「需重启」的项保存后
要重启后端进程才生效。

| 设置项 | 默认 | 生效时机 | 说明 |
|--------|------|----------|------|
| `auto_launch_ensp` | 开 | 需重启 | 后端启动时自动拉起 eNSP 并注入控制模块 |
| `auto_sync_ensp_topo` | 开 | 即时 | 切换/新建拓扑后立即让 eNSP 加载对应 `.topo` |
| `watch_ensp_topology` | 开 | 即时 | 轮询 eNSP，检测到用户在 eNSP 里换了拓扑时提示对齐或导入 |
| `ensp_poll_interval_ms` | 3000 | 即时 | 前端轮询 eNSP 实时状态的间隔 |
| `auto_start_devices` | 开 | 即时 | 一键应用拓扑后自动开机 |

设置值存在项目根目录 `app_settings.json`（已 gitignore），也可直接编辑该文件。

---

## 访问安全（重要）

**本版本没有访问令牌机制**：`services/access_guard.py` 已删除，`main.py` 不挂载令牌
中间件，所有 `/api/*` 直接可用，无需 `X-Access-Token` 头。

因此启动时请绑定回环地址，不要暴露到局域网：

```cmd
uvicorn main:app --host 127.0.0.1 --port 8000
```

真正的安全边界在**下发链路**：任何配置变更都必须走
「自动备份 → 预览 → 人工确认 → 下发后验证」，AI 只能产出受 schema 约束的 JSON plan
（`POST /api/ai/plan`），校验通过后仍要人点确认，不会自动执行。

---

## 安装步骤

### 1. 创建虚拟环境

```cmd
cd D:\ensp-web
python -m venv venv
```

### 2. 激活虚拟环境

```cmd
venv\Scripts\activate
```

### 3. 安装依赖

```cmd
pip install -r requirements.txt
```

### 4. 构建前端（首次运行前执行一次；改了 frontend/ 源码后重新执行）

```cmd
cd frontend
npm install
npm run build
cd ..
```

> `frontend/dist/` 已加入 .gitignore，全新克隆后必须先构建前端；
> 未构建时后端自动回退到旧版界面（`/legacy` 同样可访问）。
> 前端开发调试可用 `cd frontend && npm run dev`（自带 /api 代理到 127.0.0.1:8000）。

### 5. 启动项目

```cmd
python main.py
```

默认打开**桌面窗口**（系统 WebView2），无需浏览器。

调试前端时用浏览器模式（有 DevTools / React DevTools / HMR）：

```cmd
python main.py --web
```

窗口内调试：`python main.py --debug`（WebView2 开发模式，右键可检查元素）。

> `pywebview` 未安装时会提示；此时只能用 `--web` 模式，或 `pip install pywebview`。

首次访问会弹出访问令牌输入框，粘贴服务启动日志中打印的令牌即可。

---

## API 接口

### 基础接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 返回前端页面 |
| GET | `/api/devices` / POST / PUT / DELETE | 设备增删改查 |
| GET | `/api/devices/{ip}/protocols` | 已配置协议列表 |
| GET/POST/DELETE | `/api/devices/{ip}/protocols/{protocol}` | 单协议配置查询/下发/删除 |
| POST | `/api/ping` | 测试设备 Console 端口连通性 |
| POST | `/api/collect/{ip}` / `/api/collect-all` | 采集设备信息（单台/并行批量） |
| GET | `/api/collection-results` | 采集结果 |
| GET | `/api/server-data` | 一次性取回全部缓存数据（前端刷新用） |
| GET | `/api/init-status` / POST `/api/trigger-init` | 启动自动初始化状态/触发 |
| GET | `/api/ensp/status` | eNSP 运行状态与支持的设备型号 |
| POST | `/api/ensp/topo/apply` | 一键应用拓扑（`preset=comprehensive_campus` 加载内置案例）并自动开机 |
| POST | `/api/ensp/start-all` | 同步当前设备到 eNSP 并全部开机 |
| POST | `/api/ensp/stop-all` | 停止 eNSP 中全部设备 |
| GET | `/api/health` | 健康检查（免令牌） |

### 拓扑库与连线

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/topology/library` | 列出工作目录下所有拓扑 + 当前激活 |
| POST | `/api/topology/library/activate` | 切换激活拓扑 |
| POST | `/api/topology/library/create` | 新建拓扑 |
| DELETE | `/api/topology/library/{name}` | 删除拓扑（含其配置目录） |
| POST | `/api/workspace/root` | 设置工作目录 |
| GET | `/api/topology/links` | 当前拓扑的连线列表 |
| POST | `/api/topology/links` | 新增连线（校验接口存在/串口对串口/占用） |
| DELETE | `/api/topology/links` | 删除连线 |
| GET | `/api/topology/interfaces/{device}` | 某设备可用接口（供连线编辑器下拉） |

### 能力发现 / 协议角色 / 连通性

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/devices/{ip}/capabilities` | 单台设备能力识别（三态 + 证据） |
| POST | `/api/devices/capabilities-all` | 批量并行能力识别 |
| GET | `/api/devices/capability-templates` | 按设备角色获取配置模板 |
| GET | `/api/capability-results` | 缓存的能力识别结果 |
| GET | `/api/devices/{ip}/protocol-tree` | 零副作用 ? 命令树协议发现（本地 Console 通道） |
| GET | `/api/devices/{ip}/protocol-roles` | 单台设备协议角色解析 |
| POST | `/api/protocol-roles-all` | 批量协议角色（纯内存，零连接） |
| POST | `/api/devices/{ip}/device-ping` | 设备侧 ping（`target`/`count`/`source`） |

### 配置下发

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config-templates` | 配置模板列表 |
| POST | `/api/devices/{ip}/config/preview` / `config/deploy` | 模板配置预览/下发 |
| POST | `/api/devices/{ip}/config/raw-preview` / `raw-deploy` | 原始命令预览/下发 |
| POST | `/api/console-config/preview` / `deploy` / `raw-deploy` | Console 通道配置 |
| GET | `/api/config/clone/domains` | 配置复制功能域 |
| POST | `/api/config/clone/preview` / `clone/deploy` | 设备配置复制方案/下发 |
| GET | `/api/devices/{ip}/current-config` | 完整配置（离线回退最新备份并标注来源） |
| POST | `/api/config/free-preview` / `free-deploy` | 自由配置工作台 |
| POST | `/api/config/smart-defaults` | 智能默认值（Router ID/反掩码等） |
| POST | `/api/utils/network-to-wildcard` | 掩码转反掩码 |
| POST | `/api/config/check-batch-conflicts` | 跨设备批量冲突检测 |
| GET/POST/DELETE | `/api/scripts` / `/api/tasks` | 脚本保存与任务历史 |

### 命令目录与探测

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/command-catalog` | 命令目录（字段/模板/trigger） |
| POST | `/api/command-catalog/render` / `deploy` | 目录渲染/下发 |
| POST | `/api/command-catalog/probe` | 触发 ? 探测（后台任务，立即返回 job_id） |
| GET | `/api/command-catalog/probe/{job_id}` | 轮询探测结果 |

### 备份

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/devices/{ip}/backup` | 备份指定设备配置 |
| GET | `/api/devices/{ip}/backups` | 查看历史备份列表 |

### 设备启停与拓扑联动

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ensp/start-all` / `/api/ensp/stop-all` | 一键启停 eNSP 中全部设备 |
| POST | `/api/ensp/devices/{ip}/start` | **单台开机**（按设备名选中后下发启动命令） |
| POST | `/api/ensp/devices/{ip}/stop` | **单台停机** |
| POST | `/api/ensp/attach-active` | 把 eNSP 当前拓扑对齐到项目：`match_by_devices` 切换到设备名一致的已有拓扑；`import_as` 导入为新拓扑（连线需手动补） |
| GET/POST | `/api/settings` | 读取 / 更新联动开关（见「项目设置」） |

### 数据包走向与批量配置

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/path-trace` | 逻辑路径追踪：`{source_ip, dest_ip, device_ips, live}`，推导经过哪些设备、在哪断，附断点原因 |
| POST | `/api/config/free-preview` | 自由配置预览；请求体带 `series` 时按序号展开成逐台变量 |
| POST | `/api/config/free-deploy` | 批量下发；同样支持 `series`（备份→预览→确认→验证链路不变） |

`series` 形如 `{"base":{"router_id":"192.168.1.{i}"},"start":1,"step":1,"width":0,
"per_device_overrides":{"127.0.0.1:2002":{"router_id":"9.9.9.9"}}}`，
序号只按 `target_devices` 的顺序分配；模板支持 `{i}`、`{i:02d}`、`{ip_last}`、`{ip}`。

### AI 助教

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/ai/status` | 配置状态（Key 打码） |
| POST | `/api/ai/config` | 保存配置（Key 留空保留原值） |
| POST | `/api/ai/test` | 测试模型服务连通性 |
| POST | `/api/ai/chat` | 对话（`device_ip` 注入上下文；`stream=true` 返回 SSE） |
| POST | `/api/ai/diagnose` | 报错诊断（非流式） |
| GET | `/api/ai/lab/experiments` | 实验目录 |
| POST | `/api/ai/lab/start` | 开始实验会话 |
| GET | `/api/ai/lab/{id}/progress` | 进度校验（零连接） |
| POST | `/api/ai/lab/{id}/hint` | 向助教要引导提示（可流式） |
| POST | `/api/ai/plan` | 让 AI 产出**结构化 plan**（`{message, device_ip, reply}`），校验通过后返回方案摘要；**不执行任何下发** |

`/api/ai/plan` 的安全边界：AI 必须输出受 schema 约束的 JSON（`kind=config` 或 `topology`），
后端校验设备是否存在、型号是否支持、接口是否被重复占用等；校验失败会返回可读原因。
通过校验也**只是拿到一份建议**，实际下发仍要走「备份 → 预览 → 人工确认 → 验证」。

---

## 内置案例

| 预设名 | 规模 | 用途 |
|--------|------|------|
| `comprehensive_campus` | 7 台 | 综合园区网（R1/R2/SW1/AC1/SW2 + 2 台 PC） |
| `big_campus` | **15 台** | 分层园区网：出口防火墙 + 3 核心路由器（OSPF 骨干成环）+ 3 汇聚三层 + 3 接入二层 + 无线 AC/AP + 3 终端，共 16 条连线 |

`big_campus` 用「设备管理」页的「加载 15 台分层园区网并开机」一键导入，
或直接 `POST /api/ensp/topo/apply {"preset":"big_campus"}`。拓扑定义见
`tools/big_campus.py`（含接口分配注释与网段规划）。

它的 5 台三层设备（R1/R2/R3/SW1/SW2）正好用于验证批量变量化下发：
`series.base={"router_id":"192.168.1.{i}"}` 会依次生成 `192.168.1.1` ~ `192.168.1.5`。

---

## 项目结构

```
ensp-web/
├── main.py                      # FastAPI 应用装配（挂载路由与静态资源，无领域逻辑）
├── pytest.ini                   # pytest 配置（testpaths / warnings）
├── requirements.txt             # Python 依赖
├── app_settings.json            # 用户设置：工作目录（运行时生成，已 gitignore）
├── workspaces/                  # 工作目录（可改），一个拓扑一个子目录
│   ├── .current                 # 当前激活的拓扑名
│   └── campus/                  # 一个拓扑 = 一个目录
│       ├── campus.topo          # eNSP 拓扑（设备 + 连线）
│       ├── devices.json         # 项目设备表
│       ├── links.json           # 项目连线表
│       └── configs/             # 设备配置副本
├── .instance.lock / .owner      # 单实例锁与占用者信息（运行时生成，已 gitignore）
├── devices.example.json         # 设备列表示例
├── ai_config.json               # AI 接入配置（运行时生成，已 gitignore）
├── capability_cache.json        # 能力识别缓存（运行时生成，已 gitignore）
├── lab_sessions.json            # AI 实验会话（运行时生成，已 gitignore）
├── backups/                     # 设备配置备份（运行时生成，已 gitignore）
├── app/                         # 应用层（无领域依赖的基础设施）
│   ├── config.py                # 日志、路径常量、eNSP/华为设备默认参数、版本号
│   ├── state.py                 # 共享可变状态（state / init_state / _lab_sessions）
│   └── models.py                # Pydantic 请求模型
├── domain/                      # 领域层（网络设备操作的纯逻辑）
│   ├── device_store.py          # 设备列表持久化（filelock）
│   ├── ensp_runtime.py          # .topo 生成/加载、eNSP 注入、全部/单台启停控制
│   ├── ensp_guard.py            # eNSP 看护：进程出现则注入控制模块（反向联动）
│   ├── console_connection.py    # 本地 Console 连接器（设备 CLI 会话）
│   ├── console.py               # Console 下发（raw socket）
│   ├── collect.py               # 设备采集与 fast_send_command
│   ├── reachability.py          # Console 端口可达性探测
│   ├── parse.py                 # display 输出解析、配置段提取
│   ├── workspace.py             # 工作目录与拓扑库（一个拓扑一个子目录）
│   └── config_push.py           # 删除命令生成与配置推送
├── routes_*.py                  # HTTP 路由（薄层：system / devices / capabilities / ai / config / scripts）
├── frontend/                    # React 19 + Mantine 9 + TypeScript 前端（Vite 构建）
│   ├── index.html
│   ├── vite.config.ts           # base=./；dev 模式代理 /api → 127.0.0.1:8000
│   ├── tsconfig.json            # 严格模式；references 指向 tsconfig.node.json
│   ├── src/
│   │   ├── App.tsx              # 应用外壳：导航 / 主题 / 皮肤
│   │   ├── store.tsx            # 全局状态（设备/采集/能力/角色/级联链）
│   │   ├── api.ts               # API 客户端（JSON 请求 + SSE 流式）
│   │   ├── types.ts             # 领域类型（镜像后端 payload）
│   │   ├── views/               # 六个功能视图 + config/ 配置下发子面板
│   │   ├── components/          # AI 助教抽屉、表单字段、下发结果渲染
│   │   ├── lib/                 # 独立于 React 的纯函数（便于单测）
│   │   └── __tests__/           # vitest 单元测试
│   ├── node_modules/            # （npm install 生成，已 gitignore）
│   └── dist/                    # （npm run build 产物，已 gitignore）
├── services/                    # 服务模块
│   ├── ai_assistant.py          # AI 助教（接入/上下文/实验指导）
│   ├── ai_plan.py                # AI 结构化 plan（schema 校验，AI 只建议不执行）
│   ├── app_settings.py          # 用户设置项定义（开关面板的数据源）
│   ├── backup_service.py        # 配置备份
│   ├── capability_matrix.py     # eNSP 镜像 → 协议能力矩阵
│   ├── capability_probe.py      # 能力探测（矩阵/?树/多候选整合）
│   ├── command_catalog.py       # 命令目录（字段/模板/trigger）
│   ├── command_probe.py         # ? 探测引导（本地 Console）
│   ├── config_builder.py        # 声明式模板渲染引擎 + CONFIG_TEMPLATES
│   ├── config_clone_service.py  # 配置复制与自动冲突适配
│   ├── conflict_detector.py     # 跨设备冲突检测
│   ├── deploy_service.py        # 配置下发
│   ├── device_connector.py      # Console 连接工厂 + ANSI 清理
│   ├── free_config_executor.py  # 自由配置执行
│   ├── free_config_renderer.py  # 自由配置渲染/危险命令检查
│   ├── help_tree.py             # 递归 ? 视图采集与协议签名
│   ├── live_check.py            # 设备侧 ping
│   ├── path_trace.py             # 逻辑路径追踪（ARP/MAC/路由表推导路径与断点）
│   ├── role_detector.py         # 协议角色解析
│   ├── script_service.py        # 脚本保存
│   ├── smart_defaults.py        # 智能默认值
│   ├── state_persist.py         # 能力缓存/实验会话持久化
│   └── task_history_service.py  # 任务历史
├── tests/                       # 单元测试（全离线）
│   ├── conftest.py              # sys.path 注入与共享样例
│   └── test_*.py                # 按模块划分的测试
├── tools/                       # eNSP 运行时与运维脚本
│   ├── gen_topology.py          # 设备表 + 连线 → eNSP .topo（含 COMPREHENSIVE_DEMO 预设）
│   ├── big_campus.py            # 15 台分层园区网案例（核心/汇聚/接入 + 出口 + 无线 + 终端）
│   ├── ensp_control.cpp/.dll    # 注入 eNSP 的 32 位控制模块（RPC 18800）；含单台启停指令
│   ├── demo_topo/               # 生成的拓扑与连线快照（active_links.json）
│   ├── ensp_probe.py            # 扫描 Console 端口（只读）
│   ├── ensp_live_probe.py       # 真机 ? 探测验证（只读）
│   ├── ensp_live_deploy.py      # 真机下发全链路验证（会改配置）
│   ├── d_console_verify.py      # 零配置设备 Console 初始化验证（带 mock）
│   ├── inventory_probe.py       # 全量只读探测，产物写 tools/inventory/
│   └── builder_verify.py        # 配置模板渲染自检
└── static/                      # 旧版（原生 JS）前端，/legacy 入口可访问
    ├── index.html               # 旧版页面
    ├── style.css                # 旧版样式
    └── app.js                   # 旧版 JavaScript
```

**分层约定**：`app/`（基础设施）← `domain/`（领域逻辑）← `routes_*.py`（HTTP 薄层）。
`services/` 为内聚的服务模块，被 domain/routes 显式导入。所有模块间依赖均为显式 import，
不使用通配符导入。

---

## 故障排查

| 检查项 | 说明 |
|--------|------|
| eNSP 未运行 | 添加设备时会自动拉起 `eNSP_Client.exe`；手动启动亦可 |
| 注入失败 | `ensp_control.dll` 必须存在于 `tools/`；eNSP 客户端需为 32 位且与逆向版本一致 |
| 设备未开机 | 「全部开机」触发 `start_all`；开机后 Console 端口才监听 |
| Console 端口占用 | 端口从 2000 起分配，冲突时在设备表里改 `console_port` |
| 端口在线状态 | 设备详情页「端口在线」= `127.0.0.1:<port>` 可连；离线说明设备没开机 |
| 设备型号 | `model` 必须是 `tools/gen_topology.py` 支持的型号，否则拓扑生成报错 |
| AI 未配置 | AI 面板走本地降级解读；完整能力在"设置"里接入模型服务 |

---

## 测试

```cmd
python -m pytest
```

单元测试覆盖：能力矩阵、? 树解析、ANSI 清理、协议角色、填空预填、
AI 上下文与实验校验、状态持久化（含实验会话重启恢复）、
设备侧 ping 解析、Console 端口分配与拓扑同步等（全部离线运行，不接触任何真机）。

配置见 `pytest.ini`：`testpaths = tests`，仅收集 `test_*.py`。

**目录约定**：`tests/` 只放离线单元测试；连真机的运维脚本在 `tools/`，
需手动执行（多数在 `if __name__ == "__main__":` 下运行，import 无副作用）。

---

## 技术栈

- **后端：** Python、FastAPI、psutil、filelock、httpx（AI 接入）
- **前端：** React 19 + Mantine 9 + TypeScript（Vite 构建）；旧版原生 JS 界面保留于 /legacy
- **数据存储：** JSON 文件 + filelock + 内存缓存
- **AI：** OpenAI 兼容协议，模型服务自行接入
- **设计特性：** 玻璃态 UI、三模式主题、能力证据徽标、填空式配置
