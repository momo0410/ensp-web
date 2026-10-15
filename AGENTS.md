# AGENTS.md

本文件为在本仓库工作的 AI 助手与贡献者提供项目常识、命令与**测试/验证约定**。
`README.md` 描述功能全貌；`docs/功能规划与可行性评估.md` 记录需求基线与路线图。
**README 已有部分与代码脱节**（详见文末「已知脱节项」），以代码为准。

---

## 一、项目是什么

eNSP Web 网络设备管理系统：Python(FastAPI) 后端 + React 19 前端。
由本程序生成 eNSP 拓扑文件（`.topo`）并注入 eNSP 客户端自动开机，再通过
**本地 Console 端口**（`127.0.0.1:<com_port>`）连接华为路由/交换机，
采集信息、识别协议能力、解析协议角色、下发配置，并内置 AI 助教。

**硬性运行约束：**
- 后端**必须运行在装有 eNSP 的 Windows 主机**上，且需**管理员权限**
  （非管理员无法向以管理员运行的 eNSP 注入控制模块，`OpenProcess` 会被拒绝）。
- 设备接入点是 `127.0.0.1:<console_port>`，**不需要管理 IP / SSH / 同网段网卡**。
- **单实例**：同一工作目录同时只能跑一个后端进程（`.instance.lock`），重复启动会被拒绝。
- 设备启停依赖 `tools/ensp_control.dll` 注入 eNSP 客户端（RPC 端口 18800）。
- **AI Agent 依赖 Node.js**：Agent 循环跑在 Node 的 `@earendil-works/pi-agent-core` 上
  （`agent_node/`，由后端按需拉起为本地 sidecar）。工具实现仍在 Python 侧，
  sidecar 每次调用回调 `/api/agent/internal/tools/execute`。缺 Node 或依赖未安装时
  `/api/agent/chat` 返回 503（**不静默降级**），`GET /api/agent/status` 可查就绪状态。

---

## 二、常用命令

```cmd
:: 后端依赖与启动
pip install -r requirements.txt
python main.py                :: 桌面窗口（系统 WebView2）
python main.py --web          :: 浏览器模式（调试前端，含 HMR/DevTools）
python main.py --debug        :: 桌面窗口 + WebView2 开发模式
python main.py --no-ensp      :: 启动时不自动拉起 eNSP

:: 后端单元测试（全离线，不碰真机）
python -m pytest

:: 前端
cd frontend
npm install
npm run dev                   :: 开发服务器，/api 代理到 127.0.0.1:8000
npm run build                 :: tsc --build && vite build（改前端后必须重新构建）
npm test                      :: vitest run（前端单测）
npm run lint                  :: oxlint
cd ..
```

> 前端源码改动后**必须 `npm run build`**，否则后端仍返回 `frontend/dist` 旧产物
> （未构建时回退旧版 `/legacy`）。

---

## 三、目录与分层

```
main.py            FastAPI 装配（挂载路由与静态资源，无领域逻辑）
app/               基础设施：config / state / models / instance_lock / desktop
domain/            领域逻辑：workspace / ensp_runtime / console_connection / collect / parse ...
routes_*.py        HTTP 薄层：system / devices / capabilities / ai / config / scripts
services/          内聚服务：ai_assistant / capability_* / config_builder / command_catalog ...
frontend/          React 19 + Mantine 9 + TS（src/views, src/components, src/lib, src/__tests__）
static/            旧版原生 JS 界面（/legacy，已冻结，仅修致命问题）
tests/             离线单元测试（testpaths = tests）
tools/             连真机的运维脚本 + eNSP 注入控制 DLL
docs/              评估与规划文档
```

**分层约定（重要）：**
- 依赖方向：`app/`（基础设施）← `domain/`（领域逻辑）← `routes_*.py`（HTTP 薄层）。
- `services/` 为内聚服务模块，可被 domain/routes 显式导入。
- 所有模块间依赖必须**显式 import**，禁止通配符导入。
- 路由层保持薄：只做参数校验、调 domain/services、返回响应，不写领域逻辑。

**代码风格：**
- **不要主动添加注释**，除非确有必要或有明确要求。
- 不要主动创建 markdown 文档（除非被要求）。
- 修改前先看相邻文件，沿用既有约定与库（前端用 Mantine，不引新 UI 库）。

---

## 四、测试与验证（核心约定）

**原则：逻辑用离线单测；涉及 eNSP 联动的功能，走「启动服务 → 调 HTTP API → 观察 eNSP 侧真实反应」这条端到端路径验证。禁止用 mock 通过来替代对 eNSP 真实反应的确认。**

### 4.1 主路径：服务级端到端（涉及 eNSP / 设备 / 下发的改动）

这是**默认验证方式**：项目启动后间接操作 eNSP，观察 eNSP 是否真的收到并执行。

1. **确认环境**：Windows + eNSP 已安装；以管理员身份启动。
2. **启动服务**（后台）：
   ```cmd
   python main.py --web --no-ensp
   ```
   或只跑 HTTP：`uvicorn main:app --host 127.0.0.1 --port 8000`
   （`--no-ensp` 便于先测纯 HTTP 逻辑，再看 eNSP 联动）。
3. **通过 HTTP API 操作项目**（而非直接操作 eNSP）——所有 `/api/*` 走 `127.0.0.1:8000`。
   > 注意：当前 `main.py` **未挂载访问令牌中间件**，无需 `X-Access-Token`；
   > 若日后恢复令牌校验，需带上 `X-Access-Token` 头。
4. **验证 eNSP 侧真实反应**：
   - 打开/切换拓扑 → 确认 eNSP 客户端**真的加载了该 `.topo`**（比对设备列表）。
   - 启动/停止设备 → 确认 eNSP 里设备电源状态**真的变化**、Console 端口**真的开始监听**。
   - 采集/下发 → 确认读回的是**设备真实输出**，不是缓存/伪造。
   - 可用只读观测接口交叉验证：`GET /api/ensp/live`、`GET /api/ensp/status`。
5. **关键接口**（端到端回归的最小集合）：
   ```
   GET  /api/health
   GET  /api/ensp/status
   GET  /api/ensp/live
   POST /api/ensp/topo/apply        { preset | devices, auto_start }
   POST /api/ensp/start-all  /  /api/ensp/stop-all
   GET  /api/devices
   POST /api/collect-all
   POST /api/devices/capabilities-all
   ```

### 4.2 次路径：离线单元测试（纯逻辑改动）

不触真机的解析、渲染、探测整合、模型等逻辑，写 `tests/test_*.py`：

```cmd
python -m pytest
```

- `tests/` **只放离线单测**，任何连真机的脚本放 `tools/`，并置于
  `if __name__ == "__main__":` 下运行（import 无副作用）。
- 前端纯函数逻辑写 `frontend/src/__tests__/`，`cd frontend && npm test`。

### 4.3 完成标准（DoD）

改动完成前必须满足：
- [ ] 后端逻辑改动：`python -m pytest` 全绿。
- [ ] 前端改动：`npm run build` 成功；纯函数改动 `npm test` 全绿；`npm run lint` 无新增告警。
- [ ] **涉及 eNSP 联动的改动：实际启动服务、调 API、确认 eNSP 侧反应符合预期**（4.1）。
  报告时给出「启动命令 + 调用的接口 + eNSP 侧观察到的结果」，不要只说"已实现"。

---

## 五、eNSP 联动相关常识

- **正向联动**：项目启动 → 拉起 eNSP。由 `main.py: _auto_launch_ensp` 触发，
  受设置项 `auto_launch_ensp` 与 `--no-ensp` 控制。
- **拓扑生成**：`tools/gen_topology.py` 由设备表 + 连线生成 `.topo`；
  权威拓扑源是生成的 `.topo` 与 `tools/demo_topo/active_links.json`，
  **不反向从设备推拓扑**。
- **反向感知**：`read_ensp_topology()` / `probe_ensp_live()` / `compare_with_active_topology()`
  已能只读观测 eNSP 当前拓扑与设备电源状态。
- **DLL 边界**：`tools/ensp_control.cpp` 实现 `start_all` / `stop_all` / `status` /
  `live_topo` / `start_dev:<名称>` / `stop_dev:<名称>` 六条指令，依赖硬编码偏移，
  **必须与 eNSP 版本严格匹配**，改动需重新编译 32 位 DLL（编译命令见 `.cpp` 文件头）。
  改 DLL 类需求先做 spike 验证。
- **DLL 不入库**：`tools/*.dll` 是编译产物，已列入 `.gitignore`；仓库只存 `.cpp` 源码。
  部署到新机器后若缺 DLL，按文件头的 g++ 命令重新编译（需 32 位 TDM-GCC）。
- 注入/开机失败应**降级为非阻塞告警**，不得拖垮服务启动。

---

## 六、安全红线

- **AI 只建议不执行**：AI 输出只能进入预览/工作台，下发必须走
  **备份 → 预览 → 确认 → 验证**链路，确认闸门不可省略。
- 危险命令拦截、下发前自动备份、逐台失败回滚，均不可绕过。
- 不得在代码或提交中引入密钥/令牌。

---

## 七、已知脱节项（改文档前先核对代码）

- `README.md`「访问安全」整章**已失效**：`services/access_guard.py` 已删除，
  `main.py` 无访问令牌中间件。
- `domain/topology.py` 已由 `domain/workspace.py` 接管；README 结构树中
  `device_store.py` 重复两行。
- 修改 README 或文档时，以实际代码路径与行为为准，不要照抄旧描述。
