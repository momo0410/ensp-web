# eNSP Web 网络设备管理系统

通过 SSH 连接 eNSP 中的华为路由器/交换机，采集设备信息并展示在 Web 页面。

---

## 项目背景

本系统用于管理 eNSP（Enterprise Network Simulation Platform）中模拟的华为网络设备。

**关键说明：**
- Python 后端**必须运行在安装 eNSP 的 Windows 主机上**
- eNSP Cloud 本身**没有 IP**，它只是桥接工具
- Windows 虚拟网卡 IP 需与 eNSP 设备管理 IP 在同一网段；示例使用 `192.168.100.1/24`
- 如果使用 VirtualBox Host-Only 默认网卡，也可能是 `192.168.56.1/24`，设备管理 IP 应配置为同网段地址，例如 `192.168.56.x/24`
- 后端通过 **SSH 连接 eNSP 设备的管理 IP**，不是连接 eNSP 软件本身
- Web 前端无法直接读取网卡，网卡识别由 Python 后端完成

---

## 功能列表

1. 自动识别本机 Windows 网卡信息
2. 智能识别疑似 eNSP 管理网卡（优先匹配 192.168.100.1、VirtualBox Host-Only、VMware 等虚拟网卡）
3. 支持用户手动选择管理网卡
4. 根据所选网卡自动推算管理网段
5. 扫描网段中的在线设备（并发 ping，50 线程）
6. 对在线设备尝试 SSH 登录（Netmiko）
7. 通过 SSH 采集华为设备信息（10 条常用命令）
8. 每条命令独立异常捕获，单条失败不影响其他
9. SSH 自动重试机制（2 次，指数退避）
10. 将设备信息输出到 Web 页面（卡片式折叠展示）
11. 根据 LLDP 信息生成拓扑关系
12. 支持批量采集所有设备信息（并行执行）
13. 支持手动添加/删除设备
14. 支持测试设备连通性
15. 支持浅色/深色/跟随系统三模式主题切换
16. JSON 文件持久化，带 filelock 并发保护

---

## 采集的华为命令

| 命令 | 用途 |
|------|------|
| `display version` | 设备版本信息 |
| `display ip interface brief` | 接口 IP 概要 |
| `display current-configuration` | 当前配置 |
| `display vlan` | VLAN 信息 |
| `display ip routing-table` | 路由表 |
| `display ospf peer` | OSPF 邻居 |
| `display lldp neighbor brief` | LLDP 邻居概要 |
| `display device` | 设备硬件信息 |
| `display cpu-usage` | CPU 使用率 |
| `display memory-usage` | 内存使用率 |

---

## 安装步骤

### 1. 创建虚拟环境

```cmd
cd D:\ensp-web
python -m venv venv
```

### 2. 激活虚拟环境

**Windows：**
```cmd
venv\Scripts\activate
```

### 3. 安装依赖

```cmd
pip install -r requirements.txt
```

如果 `filelock` 安装失败，不影响核心功能（仅丢失并发保护），可跳过：
```cmd
pip install fastapi uvicorn psutil netmiko ping3
```

### 4. 启动项目

```cmd
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

首次运行时会自动创建本地 `devices.json`。仓库中的 `devices.example.json` 仅作示例；真实设备 IP、用户名和密码请在 Web 页面中添加，或复制示例后自行修改。

### 5. 浏览器访问

打开浏览器，访问：
```
http://127.0.0.1:8000
```

---

## 使用指南

### 基本工作流程

1. 打开页面后，系统会自动刷新并推荐本机管理网卡，也可以点击 **"刷新网卡"** 手动刷新
2. 确认推荐的 eNSP 管理网卡；如需调整，点击对应网卡的 **"设为管理网卡"**
3. 点击 **"扫描管理网段"** 发现在线设备
4. 点击 **"全部添加"** 将在线设备加入管理，或逐个点击 IP 旁边的 **+** 按钮
5. 点击 **"采集所有设备"** 批量采集（并行执行，更快完成）
6. 在设备信息详情区域点击设备卡片查看采集结果
7. 点击 **"刷新拓扑"** 查看 LLDP 拓扑

### 单台设备操作

在设备列表中，可以对单台设备执行：
- **Ping** - 测试连通性
- **采集** - SSH 连接并采集信息
- **删除** - 从列表中移除

---

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 返回前端页面 |
| GET | `/api/interfaces` | 获取本机网卡信息 |
| POST | `/api/interfaces/select` | 选择管理网卡 |
| POST | `/api/scan-by-interface` | 根据管理网卡扫描网段 |
| POST | `/api/scan` | 扫描指定网段 |
| GET | `/api/devices` | 获取设备列表 |
| POST | `/api/devices` | 添加设备 |
| DELETE | `/api/devices/{ip}` | 删除设备 |
| POST | `/api/ping` | 测试设备连通性 |
| POST | `/api/collect/{ip}` | 采集指定设备信息 |
| POST | `/api/collect-all` | 批量采集所有设备信息 |
| GET | `/api/topology` | 获取 LLDP 拓扑关系 |
| GET | `/api/collection-results` | 获取所有采集结果 |
| GET | `/api/health` | 健康检查 |

---

## 项目结构

```
ensp_web_manager/
├── main.py              # FastAPI 后端主程序
├── requirements.txt     # Python 依赖
├── devices.json         # 设备列表（JSON 文件存储）
├── README.md            # 本说明文件
├── .workbuddy/          # AI 工作记录（可忽略）
└── static/
    ├── index.html       # 前端页面
    ├── style.css        # 样式表（含双主题）
    └── app.js           # 前端 JavaScript
```

---

## 故障排查

如果无法采集设备信息，请依次检查：

| 检查项 | 说明 |
|--------|------|
| Windows 虚拟网卡 IP | 确认为与 eNSP 设备管理 IP 在同一网段，例如 `192.168.100.1/24` 或 `192.168.56.1/24` |
| eNSP Cloud 绑定 | Cloud 必须绑定到该虚拟网卡 |
| 设备管理 IP | eNSP 设备接口已配置管理 IP |
| ping 连通性 | Windows 应能 ping 通设备管理 IP |
| SSH 开关 | 设备已开启 SSH 服务（`stelnet server enable`） |
| 用户名密码 | 默认 `admin` / `Huawei@123` |
| 防火墙 | Windows 防火墙未拦截相关流量 |
| Netmiko 安装 | 确认已执行 `pip install netmiko` |

### eNSP 设备开启 SSH 示例

```
# 进入系统视图
system-view

# 创建本地用户
aaa
 local-user admin password cipher Huawei@123
 local-user admin service-type ssh
 local-user admin privilege level 15
quit

# 开启 SSH
stelnet server enable

# 配置 VTY 线路
user-interface vty 0 4
 authentication-mode aaa
 protocol inbound ssh
quit

# 保存配置
save
```

---

## 技术栈

- **后端：** Python、FastAPI、Netmiko、psutil、filelock
- **前端：** HTML、CSS、JavaScript（原生 fetch API，无框架）
- **数据存储：** JSON 文件（devices.json，filelock 并发保护）
- **SSH 协议：** 通过 Netmiko 连接华为设备
- **设计特性：** 玻璃态 UI、浅色/深色/跟随系统三模式主题、流畅微动效

---

## 注意事项

1. 本系统**不连接 eNSP 软件本身**，而是通过 SSH 连接 eNSP 中的网络设备
2. 所有设备必须已开启 SSH 服务
3. 扫描速度取决于网络延迟和并发数
4. 大量设备采集时，并行采集会更快完成（最长耗时设备决定总时间）
5. `devices.json` 文件会被自动创建和维护
6. 采集结果保存在内存中，重启后需要重新采集
7. 切换主题：点击右上角的主题按钮，按 浅色 → 深色 → 跟随系统 循环切换
