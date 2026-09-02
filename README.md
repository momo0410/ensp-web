# eNSP Web 网络设备管理系统

通过 SSH 连接 eNSP 中的华为路由器/交换机，采集设备信息、识别设备能力、
解析协议角色、以填空式表单下发配置，并内置可自行接入模型服务的 **AI 助教**。

---

## 项目背景

本系统用于管理 eNSP（Enterprise Network Simulation Platform）中模拟的华为网络设备。

**关键说明：**
- Python 后端**必须运行在安装 eNSP 的 Windows 主机上**
- eNSP Cloud 本身**没有 IP**，它只是桥接工具
- Windows 虚拟网卡 IP 需与 eNSP 设备管理 IP 在同一网段（常见 `192.168.56.1/24` 或 `192.168.100.1/24`）
- 后端通过 **SSH 连接 eNSP 设备的管理 IP**，不是连接 eNSP 软件本身
- LLDP 拓扑发现需要设备开启 `lldp enable`
- 交换机无法 SSH 时，先确认 Vlanif 管理 IP、VTY、AAA、`stelnet server enable`

---

## 快速开始（五步上手）

1. **网卡识别** → 选择管理网卡（与设备同网段的虚拟网卡）
2. **发现设备** → 扫描网段，Ping + SSH 双重探测，自动入库
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
探测延迟有多层保障：采集缓存优先（可零 SSH）、矩阵跳过无效探测、候选命令懒执行、
`?` 树采集有硬性时间预算、探测结果缓存 30 分钟（eNSP 镜像的支持集合不会变）。

### 协议角色与填空式配置

`role_detector` 纯解析已有探测输出（零 SSH），回答"这台设备在每个协议里扮演什么角色"：
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
  进度校验基于能力识别缓存（零 SSH），助教按进度给引导提示，不直接给整段答案。

---

## 功能列表

### 基础功能
1. 自动识别 Windows 网卡，智能识别疑似 eNSP 管理网卡
2. 扫描管理网段发现候选设备（Ping + SSH 双重探测）
3. 设备增删改查、并行采集 10+ 条 display 命令、LLDP 拓扑
4. JSON 持久化（filelock 并发保护）；能力缓存/实验会话同样落盘，重启不丢
5. 浅色/深色/跟随系统三模式主题

### 能力识别与角色
6. 三层证据的协议能力发现（矩阵 / ? 命令树 / display 探测），31 项协议
7. 设备角色分类（路由器/交换机/三层交换机），镜像家族与置信度展示
8. 协议角色总览页与证据徽标（?树实测 / 镜像矩阵 / 探测）
9. 零副作用 `?` 命令树协议发现接口（SSH / Console 双通道）

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
18. 访问令牌：所有 `/api/*` 接口要求 `X-Access-Token`（首次启动自动生成并打印在日志）
19. 下发历史记录（task_history），AI 配置/访问令牌均不入 git

---

## 访问安全（重要）

本系统可以真实修改设备配置，**默认启用访问令牌**：除静态页面和 `/api/health` 外，
所有接口都要求携带 `X-Access-Token` 请求头。

- 首次启动自动生成随机令牌，写入项目根目录 `web_access.json`（已加入 .gitignore），
  并在服务启动日志中打印——教师把日志里的令牌发给学生即可。
- 客户端首次打开页面会弹出令牌输入框，输入一次后保存在浏览器 localStorage。
- 自定义令牌：环境变量 `WEB_ACCESS_TOKEN=<你的令牌>`（优先级最高），
  或直接编辑 `web_access.json`。
- 关闭校验：环境变量设为 `WEB_ACCESS_TOKEN=disabled`，或 `web_access.json` 中
  `"enabled": false`——**仅建议纯本机回环使用时关闭**。

启动绑定建议：单人使用绑回环地址 `uvicorn main:app --host 127.0.0.1`；
需要在机房局域网共享时再用 `0.0.0.0`，并确保令牌只发给学生。

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

### 4. 启动项目

```cmd
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

### 5. 浏览器访问

```
http://127.0.0.1:8000
```

首次访问会弹出访问令牌输入框，粘贴服务启动日志中打印的令牌即可。

---

## API 接口

### 基础接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 返回前端页面 |
| GET | `/api/interfaces` | 获取本机网卡信息 |
| POST | `/api/interfaces/select` | 选择管理网卡 |
| POST | `/api/scan-by-interface` | 按指定网卡扫描网段 |
| POST | `/api/discover-devices` | 扫描管理网段并同步设备 |
| GET | `/api/ensp-console-diagnostics` | Console 端口诊断 |
| GET | `/api/devices` / POST / PUT / DELETE | 设备增删改查 |
| POST | `/api/ping` | 管理面测试设备连通性 |
| POST | `/api/collect/{ip}` / `/api/collect-all` | 采集设备信息（单台/并行批量） |
| GET | `/api/topology` | LLDP 拓扑关系 |
| GET | `/api/collection-results` | 采集结果 |
| GET | `/api/init-status` / POST `/api/trigger-init` | 启动自动初始化状态/触发 |
| GET | `/api/health` | 健康检查（免令牌） |

### 能力发现 / 协议角色 / 连通性

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/devices/{ip}/capabilities` | 单台设备能力识别（三态 + 证据） |
| POST | `/api/devices/capabilities-all` | 批量并行能力识别 |
| GET | `/api/devices/capability-templates` | 按设备角色获取配置模板 |
| GET | `/api/capability-results` | 缓存的能力识别结果 |
| GET | `/api/devices/{ip}/protocol-tree` | 零副作用 ? 命令树协议发现（`?mode=console&port=x` 走 Console） |
| GET | `/api/devices/{ip}/protocol-roles` | 单台设备协议角色解析 |
| POST | `/api/protocol-roles-all` | 批量协议角色（纯内存，零 SSH） |
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
| GET | `/api/ai/lab/{id}/progress` | 进度校验（零 SSH） |
| POST | `/api/ai/lab/{id}/hint` | 向助教要引导提示（可流式） |

---

## 项目结构

```
ensp-web/
├── main.py                      # FastAPI 后端主程序（全部路由）
├── requirements.txt             # Python 依赖
├── devices.json                 # 设备列表（运行时生成，已 gitignore）
├── devices.example.json         # 设备列表示例
├── ai_config.json               # AI 接入配置（运行时生成，已 gitignore）
├── web_access.json              # 访问令牌（运行时生成，已 gitignore）
├── capability_cache.json        # 能力识别缓存（运行时生成，已 gitignore）
├── backups/                     # 设备配置备份（运行时生成，已 gitignore）
├── services/                    # 服务模块
│   ├── access_guard.py          # 访问令牌
│   ├── ai_assistant.py          # AI 助教（接入/上下文/实验指导）
│   ├── backup_service.py        # 配置备份
│   ├── capability_matrix.py     # eNSP 镜像 → 协议能力矩阵
│   ├── capability_probe.py      # 能力探测（矩阵/?树/多候选整合）
│   ├── cli_completer.py         # CLI 补全
│   ├── command_catalog.py       # 命令目录（字段/模板/trigger）
│   ├── command_probe.py         # ? 探测引导（双通道）
│   ├── config_builder.py        # 声明式模板渲染引擎 + CONFIG_TEMPLATES
│   ├── config_clone_service.py  # 配置复制与自动冲突适配
│   ├── conflict_detector.py     # 跨设备冲突检测
│   ├── custom_template_service.py # 自定义模板
│   ├── deploy_service.py        # 配置下发
│   ├── device_connector.py      # SSH 连接管理 + ANSI 清理
│   ├── free_config_executor.py  # 自由配置执行
│   ├── free_config_renderer.py  # 自由配置渲染/危险命令检查
│   ├── help_tree.py             # 递归 ? 视图采集与协议签名
│   ├── live_check.py            # 设备侧 ping
│   ├── role_detector.py         # 协议角色解析
│   ├── script_service.py        # 脚本保存
│   ├── smart_defaults.py        # 智能默认值
│   ├── state_persist.py         # 能力缓存/实验会话持久化
│   └── task_history_service.py  # 任务历史
└── static/
    ├── index.html               # 前端页面
    ├── style.css                # 样式表
    └── app.js                   # 前端 JavaScript
```

---

## 故障排查

| 检查项 | 说明 |
|--------|------|
| Windows 虚拟网卡 IP | 确认与 eNSP 设备管理 IP 在同一网段 |
| eNSP Cloud 绑定 | Cloud 必须绑定到对应虚拟网卡 |
| Cloud 端口映射 | Console 可见但 ping 不通时，检查 UDP 端口映射 |
| 设备管理 IP | eNSP 设备接口已配置管理 IP |
| ping 连通性 | Windows 应能 ping 通设备管理 IP（也可用设备侧 ping 反向验证） |
| SSH 开关 | 设备已开启 SSH 服务（`stelnet server enable`） |
| 用户名密码 | 默认 `admin` / `Huawei@123` |
| eNSP 设备 SSH 配置 | 需要 aaa、local-user、stelnet server enable、VTY 配置 |
| 代理/TUN 路由 | 确认管理网段未被代理拦截 |
| Windows 防火墙 | 未拦截相关流量 |
| Netmiko 安装 | `pip install netmiko` |
| LLDP | eNSP 设备需先开启 `lldp enable` 才能发现拓扑 |
| 访问令牌 | 接口 401 时，从服务启动日志或 `web_access.json` 获取令牌 |
| AI 未配置 | AI 面板走本地降级解读；完整能力在"设置"里接入模型服务 |

---

## 测试

```cmd
python -m pytest tests/ -v
```

单元测试覆盖：能力矩阵、? 树解析、ANSI 清理、协议角色、填空预填、
AI 上下文与实验校验、访问令牌、持久化、设备侧 ping 解析等（111 项，全部离线运行）。

---

## 技术栈

- **后端：** Python、FastAPI、Netmiko、psutil、filelock、httpx（AI 接入）
- **前端：** HTML、CSS、JavaScript（原生 fetch + SSE 流式）
- **数据存储：** JSON 文件 + filelock + 内存缓存
- **AI：** OpenAI 兼容协议，模型服务自行接入
- **设计特性：** 玻璃态 UI、三模式主题、能力证据徽标、填空式配置
