# eNSP Web 网络设备管理系统

通过 SSH 连接 eNSP 中的华为路由器/交换机，采集设备信息、识别设备能力、下发配置并展示在 Web 页面。

---

## 项目背景

本系统用于管理 eNSP（Enterprise Network Simulation Platform）中模拟的华为网络设备。

**关键说明：**
- Python 后端**必须运行在安装 eNSP 的 Windows 主机上**
- eNSP Cloud 本身**没有 IP**，它只是桥接工具
- Windows 虚拟网卡 IP 需与 eNSP 设备管理 IP 在同一网段；示例使用 `192.168.100.1/24`
- 如果使用 VirtualBox Host-Only 默认网卡，也可能是 `192.168.56.1/24`，设备管理 IP 应配置为同网段地址
- 后端通过 **SSH 连接 eNSP 设备的管理 IP**，不是连接 eNSP 软件本身
- 后端不是读取 eNSP 拓扑文件，而是通过 SSH 管理设备
- Web 前端无法直接读取网卡，网卡识别由 Python 后端完成
- LLDP 拓扑发现需要设备开启 `lldp enable`
- 如果交换机无法 SSH，需要先确认 Vlanif 管理 IP、VTY、AAA、`stelnet server` 是否配置正确

---

## 功能列表

### 基础功能
1. 自动识别本机 Windows 网卡信息
2. 智能识别疑似 eNSP 管理网卡
3. 扫描管理网段中的可管理候选设备（Ping + SSH 双重探测）
4. SSH 登录识别设备名称和型号
5. 采集华为设备信息（10+ 条常用 display 命令）
6. 根据 LLDP 信息生成拓扑关系
7. 批量采集所有设备信息（并行执行）
8. JSON 文件持久化，带 filelock 并发保护
9. 浅色/深色/跟随系统三模式主题切换

### 设备能力识别（v3.0 新增）
10. 通过 SSH 自动识别设备类型：路由器 / 交换机 / 三层交换机 / 未知
11. 识别设备型号和系统版本
12. 对 20+ 项协议和功能进行三状态识别：
    - **supported** - 设备是否支持该功能
    - **configured** - 当前是否已配置
    - **running** - 当前是否正在运行
13. 综合判断依据：设备型号、display 命令探测、当前配置分析、协议运行状态

### 配置下发（v3.0 新增）
14. 根据设备角色动态显示可配置功能模板
15. 支持 9 种配置模板：接口 IP、静态路由、OSPF、VLAN、Access/Trunk 接口、VLANIF、LLDP、SSH 用户
16. 配置预览：生成命令但不执行
17. 配置下发：下发前自动备份，下发后自动验证
18. 高级命令模式：直接下发原始命令（带危险警告和二次确认）
19. 可选保存配置（下发后执行 save）

### 配置备份（v3.0 新增）
20. 自动备份：每次下发配置前自动备份当前配置
21. 手动备份：一键备份指定设备配置
22. 备份文件命名格式：`设备名_IP_时间.cfg`
23. 查看历史备份列表

---

## 能力识别原理

系统通过 SSH 登录设备后，执行以下探测流程：

1. **基础信息采集** - 执行 `display version`、`display device`、`display current-configuration` 等命令
2. **设备类型分类** - 根据型号关键字（AR/S5700/S3700/Switch）和配置特征（VLAN/NAT/BGP/OSPF）综合判断
3. **能力探测** - 对每项能力执行对应 display 命令，判断 supported/configured/running
4. **不支持判断** - 识别 `Unrecognized command`、`Wrong parameter` 等关键字
5. **配置分析** - 在 `display current-configuration` 中搜索配置关键字判断 configured 状态
6. **运行状态** - 在 display 输出中搜索有效邻居、表项、状态判断 running

### supported / configured / running 三状态说明

| 状态 | 含义 | 判断依据 |
|------|------|---------|
| `supported = true` | 设备支持该功能 | display 命令执行成功，未返回 "Unrecognized command" |
| `supported = false` | 设备不支持 | display 命令返回 "Unrecognized command" 或类似错误 |
| `configured = true` | 已配置 | 当前配置中包含相关关键字 |
| `configured = false` | 未配置 | 当前配置中没有相关关键字 |
| `running = true` | 正在运行 | display 输出包含有效邻居/状态/表项 |
| `running = false` | 未运行 | 无有效输出或进程未启动 |

---

## Web 配置下发流程

1. 用户在"设备配置下发"区域选择目标设备
2. 系统根据设备角色加载对应的配置模板
3. 用户选择模板并填写表单参数
4. 点击"生成配置预览"查看将要下发的命令
5. 确认无误后点击"确认下发"
6. 系统自动备份当前配置 → 下发配置命令 → 执行验证命令 → 返回结果
7. 可选勾选"下发后保存配置"

### 为什么下发前要备份？

配置下发会修改设备运行配置。一旦下发错误命令，可能导致设备无法远程管理。备份提供了回退手段：如果配置出问题，可以通过 Console 线恢复备份文件中的配置。

### 为什么不建议直接使用高级命令模式？

高级命令模式直接下发原始命令，系统不会进行参数校验和命令优化。如果命令有误，可能导致：
- 设备配置损坏
- 管理通道中断（如误删 VTY 或 AAA 配置）
- 不可预期的网络行为

建议优先使用模板模式，仅在模板无法满足需求时使用高级命令模式。

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
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. 浏览器访问

```
http://127.0.0.1:8000
```

---

## API 接口

### 基础接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 返回前端页面 |
| GET | `/api/interfaces` | 获取本机网卡信息 |
| POST | `/api/interfaces/select` | 选择管理网卡 |
| POST | `/api/discover-devices` | 扫描管理网段并同步设备 |
| GET | `/api/ensp-console-diagnostics` | Console 端口诊断 |
| GET | `/api/devices` | 获取设备列表 |
| POST | `/api/devices` | 添加设备 |
| PUT | `/api/devices/{ip}` | 更新设备配置 |
| DELETE | `/api/devices/{ip}` | 删除设备 |
| POST | `/api/ping` | 测试设备连通性 |
| POST | `/api/collect/{ip}` | 采集指定设备信息 |
| POST | `/api/collect-all` | 批量采集所有设备信息 |
| GET | `/api/topology` | 获取 LLDP 拓扑关系 |
| GET | `/api/collection-results` | 获取所有采集结果 |
| GET | `/api/health` | 健康检查 |

### 能力识别接口（v3.0 新增）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/devices/{ip}/capabilities` | 识别指定设备能力 |
| POST | `/api/devices/capabilities-all` | 批量识别所有设备能力 |
| GET | `/api/devices/capability-templates` | 获取配置模板列表 |

### 备份接口（v3.0 新增）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/devices/{ip}/backup` | 备份指定设备配置 |
| GET | `/api/devices/{ip}/backups` | 查看设备备份列表 |

### 配置下发接口（v3.0 新增）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/devices/{ip}/config/preview` | 模板配置预览 |
| POST | `/api/devices/{ip}/config/deploy` | 模板配置下发 |
| POST | `/api/devices/{ip}/config/raw-preview` | 原始命令预览 |
| POST | `/api/devices/{ip}/config/raw-deploy` | 原始命令下发 |

---

## 项目结构

```
ensp-web/
├── main.py                  # FastAPI 后端主程序
├── requirements.txt         # Python 依赖
├── devices.json             # 设备列表（JSON 持久化）
├── README.md                # 本说明文件
├── backups/                 # 设备配置备份目录
├── services/                # 服务模块
│   ├── __init__.py
│   ├── device_connector.py  # SSH 连接管理工具
│   ├── capability_probe.py  # 设备能力识别
│   ├── config_builder.py    # 配置模板命令生成
│   ├── backup_service.py    # 配置备份
│   └── deploy_service.py    # 配置下发
└── static/
    ├── index.html           # 前端页面
    ├── style.css            # 样式表
    └── app.js               # 前端 JavaScript
```

---

## 故障排查

| 检查项 | 说明 |
|--------|------|
| Windows 虚拟网卡 IP | 确认与 eNSP 设备管理 IP 在同一网段 |
| eNSP Cloud 绑定 | Cloud 必须绑定到对应虚拟网卡 |
| Cloud 端口映射 | Console 可见但 ping 不通时，检查 UDP 端口映射 |
| 设备管理 IP | eNSP 设备接口已配置管理 IP |
| ping 连通性 | Windows 应能 ping 通设备管理 IP |
| SSH 开关 | 设备已开启 SSH 服务（`stelnet server enable`） |
| 用户名密码 | 默认 `admin` / `Huawei@123` |
| eNSP 设备 SSH 配置 | 需要 aaa、local-user、stelnet server enable、VTY 配置 |
| 代理/TUN 路由 | 确认管理网段未被代理拦截 |
| Windows 防火墙 | 未拦截相关流量 |
| Netmiko 安装 | `pip install netmiko` |
| LLDP | eNSP 设备需先开启 `lldp enable` 才能发现拓扑 |
| 交换机 SSH | 需确认 Vlanif 管理 IP、VTY、AAA、stelnet 配置正确 |

---

## 技术栈

- **后端：** Python、FastAPI、Netmiko、psutil、filelock
- **前端：** HTML、CSS、JavaScript（原生 fetch API）
- **数据存储：** JSON 文件 + 内存缓存
- **SSH 协议：** Netmiko 连接华为设备
- **设计特性：** 玻璃态 UI、三模式主题、流畅微动效
