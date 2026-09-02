/**
 * eNSP Web 管理系统 - 前端逻辑
 * ============================================================
 * 特性：
 * - 自定义模态框替代 alert
 * - 按钮加载态防止重复提交
 * - 浅色/深色/跟随系统 三模式主题
 * - 统一 API 调用层
 * - 完善的状态反馈
 * ============================================================
 */

// ============================================================
// 全局变量
// ============================================================

let currentInterfaces = [];
let currentDevices = [];
let collectionResults = {};
let selectedSubnet = "";
let editingDeviceIp = null;
let autoCollectInProgress = false;
window.__ENSP_APP_LOADED__ = true;

const API_ORIGIN = window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";

// ============================================================
// 1.5 侧边栏交互
// ============================================================

/**
 * 切换侧边栏折叠/展开状态
 */
function toggleSidebar() {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return;
    sidebar.classList.toggle("collapsed");
    // 保存状态到 localStorage
    localStorage.setItem("ensp-sidebar-collapsed", sidebar.classList.contains("collapsed"));
}

/**
 * 切换显示指定的内容区域
 * @param {string} sectionId - 要显示的 section ID
 */
function showSection(sectionId) {
    // 隐藏所有 section
    document.querySelectorAll(".section-panel").forEach(section => {
        section.classList.remove("active");
    });

    // 显示目标 section
    const targetSection = document.getElementById(sectionId);
    if (targetSection) {
        targetSection.classList.add("active");
    }

    // 更新菜单项高亮状态
    document.querySelectorAll(".menu-item").forEach(item => {
        item.classList.toggle("active", item.dataset.section === sectionId);
    });

    // 进入配置下发区时加载命令目录与设备列表
    if (sectionId === "section-config-deploy") {
        initCatalogPanel();
    }

    // 移动端点击后自动关闭侧边栏
    const sidebar = document.getElementById("sidebar");
    if (sidebar && window.innerWidth <= 900) {
        sidebar.classList.remove("mobile-open");
    }
}

/**
 * 初始化侧边栏状态
 */
function initSidebar() {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return;

    // 恢复折叠状态
    const savedCollapsed = localStorage.getItem("ensp-sidebar-collapsed");
    if (savedCollapsed === "true") {
        sidebar.classList.add("collapsed");
    }

    // 移动端点击遮罩关闭侧边栏
    document.addEventListener("click", function(e) {
        if (window.innerWidth <= 900 && sidebar.classList.contains("mobile-open")) {
            if (!sidebar.contains(e.target) && e.target.id !== "mobileMenuBtn") {
                sidebar.classList.remove("mobile-open");
            }
        }
    });
}

// ============================================================
// 1. 通用工具函数
// ============================================================

/**
 * 统一的 API 请求封装。
 * 自动处理 JSON 序列化、错误提取、非 2xx 响应。
 */
// ---------- 访问令牌 ----------
function _accessToken() {
    return localStorage.getItem("web_access_token") || "";
}

/** 弹出令牌输入框；resolve(令牌) 或 reject(用户取消) */
function _promptAccessToken() {
    return new Promise((resolve, reject) => {
        const modal = document.getElementById("access-token-modal");
        const input = document.getElementById("access-token-input");
        const err = document.getElementById("access-token-error");
        const ok = document.getElementById("access-token-ok");
        if (!modal || !input || !ok) { reject(new Error("页面缺少令牌组件")); return; }
        err?.classList.add("hidden");
        input.value = _accessToken();
        modal.classList.remove("hidden");
        setTimeout(() => input.focus(), 100);
        const done = (token) => {
            modal.classList.add("hidden");
            ok.onclick = null;
            input.onkeydown = null;
            if (token) {
                localStorage.setItem("web_access_token", token);
                resolve(token);
            } else {
                reject(new Error("未输入访问令牌，无法调用接口"));
            }
        };
        ok.onclick = () => done(input.value.trim());
        input.onkeydown = (ev) => { if (ev.key === "Enter") done(input.value.trim()); };
    });
}

function _showTokenError(message) {
    const err = document.getElementById("access-token-error");
    if (err) {
        err.textContent = message;
        err.classList.remove("hidden");
    }
}

async function api(path, options = {}, _retried = false) {
    const config = {
        headers: { "Content-Type": "application/json", "X-Access-Token": _accessToken() },
        ...options,
    };
    const url = path.startsWith("http") ? path : API_ORIGIN + path;
    const resp = await fetch(url, config);
    if (resp.status === 401 && !_retried) {
        try {
            await _promptAccessToken();
        } catch (e) {
            throw new Error("需要访问令牌：请在弹窗中输入服务端打印的令牌");
        }
        return api(path, options, true);
    }
    if (!resp.ok) {
        let detail = resp.statusText;
        try {
            const err = await resp.json();
            detail = err.detail || err.message || detail;
        } catch (_) { /* 忽略解析失败 */ }
        throw new Error(detail);
    }
    return resp.json();
}

/**
 * HTML 转义，防止 XSS。
 */
function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

/**
 * 显示状态条（支持 info / success / error / loading 类型）
 */
function showStatus(elementId, message, type = "info") {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.hidden = false;
    el.textContent = message;
    el.className = "info-bar " + type;
}

function hideStatus(elementId) {
    const el = document.getElementById(elementId);
    if (el) el.hidden = true;
}

async function getConsoleDiagnosticMessage() {
    try {
        const data = await api("/api/ensp-console-diagnostics");
        const consoleDevices = data.console_devices || [];
        if (!consoleDevices.length) {
            return "";
        }
        if (data.cloud_bridge_issue) {
            const names = (data.cloud_issue_devices || [])
                .map((item) => `${item.name}(${(item.mgmt_ips || []).join("/")})`)
                .join("、");
            return names
                ? `。Console 已检测到 ${names}，SSH 配置存在，但宿主机与管理 IP 不互通，重点检查 Cloud 到 VirtualBox Host-Only 的双向映射`
                : "。Console 已检测到设备，但宿主机与管理 IP 不互通，重点检查 Cloud 到 VirtualBox Host-Only 的双向映射";
        }
        const names = consoleDevices.map((item) => item.name).join("、");
        return `。Console 可见设备: ${names}`;
    } catch (e) {
        console.warn("Console 诊断失败:", e);
        return "";
    }
}

/**
 * 按钮加载状态管理
 */
function setButtonLoading(buttonId, loading) {
    const btn = document.getElementById(buttonId);
    if (!btn) return;
    btn.disabled = loading;
    const spinner = btn.querySelector(".btn-spinner");
    if (spinner) {
        spinner.classList.toggle("hidden", !loading);
    }
}

// ============================================================
// 2. 主题切换（浅色 / 深色 / 跟随系统）
// ============================================================

// 主题模式顺序：light -> dark -> system
const THEME_CYCLE = ["light", "dark", "system"];
const THEME_LABELS = { light: "浅色模式", dark: "深色模式", system: "跟随系统" };

function getSavedTheme() {
    return localStorage.getItem("ensp-theme") || "light";
}

function saveTheme(theme) {
    localStorage.setItem("ensp-theme", theme);
}

function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    saveTheme(theme);
}

function toggleTheme() {
    const current = getSavedTheme();
    const nextIndex = (THEME_CYCLE.indexOf(current) + 1) % THEME_CYCLE.length;
    const next = THEME_CYCLE[nextIndex];
    applyTheme(next);
}

// 初始化主题
applyTheme(getSavedTheme());

// ============================================================
// 4. 网卡信息
// ============================================================

async function loadInterfaces() {
    setButtonLoading("btn-refresh-interfaces", true);
    try {
        const data = await api("/api/interfaces");
        currentInterfaces = data.interfaces;
        renderInterfaces(data.interfaces);
        const recommended = data.recommended_interface || data.interfaces.find((iface) => iface.is_recommended);
        if (recommended && recommended.ipv4 && recommended.netmask) {
            await selectInterfaceByName(recommended.name, true);
        }
    } catch (e) {
        console.error("读取网卡失败:", e.message);
        const tbody = document.getElementById("interfaces-tbody");
        // 区分网络不通（后端未启动）和后端返回的错误
        if (e.message.includes("Failed to fetch") || e.message.includes("NetworkError")) {
            tbody.innerHTML = '<tr><td colspan="8" class="table-empty" style="color:var(--color-danger,#dc3545)">无法连接后端服务，请确认 main.py 已启动（python main.py）</td></tr>';
        } else {
            tbody.innerHTML = '<tr><td colspan="8" class="table-empty" style="color:var(--color-danger,#dc3545)">加载网卡失败: ' + escapeHtml(e.message) + '</td></tr>';
        }
    } finally {
        setButtonLoading("btn-refresh-interfaces", false);
    }
}

function renderInterfaces(interfaces) {
    const tbody = document.getElementById("interfaces-tbody");
    if (!interfaces.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="table-empty">未发现网卡</td></tr>';
        return;
    }

    tbody.innerHTML = interfaces.map((iface, index) => {
        const typeBadge = iface.is_recommended
            ? '<span class="badge badge-success">推荐</span>'
            : iface.is_ensp
                ? '<span class="badge badge-warning">疑似 eNSP</span>'
                : iface.ipv4
                    ? '<span class="badge badge-info">普通</span>'
                    : '-';
        const reason = iface.recommendation_reason
            ? `<div class="table-note">${escapeHtml(iface.recommendation_reason)}</div>`
            : "";
        return `<tr class="${iface.is_recommended ? "interface-recommended" : ""}">
            <td>
                <div class="interface-name">${escapeHtml(iface.name)}</div>
                ${iface.description ? `<div class="interface-desc">${escapeHtml(iface.description)}</div>` : ""}
            </td>
            <td>${escapeHtml(iface.ipv4 || "-")}</td>
            <td>${escapeHtml(iface.netmask || "-")}</td>
            <td>${escapeHtml(iface.mac || "-")}</td>
            <td>
                <span class="badge ${iface.status === "UP" ? "badge-success" : "badge-danger"}">
                    ${iface.status}
                </span>
            </td>
            <td>${escapeHtml(iface.subnet || "-")}</td>
            <td>
                ${typeBadge}
                ${reason}
            </td>
            <td>
                <button class="btn btn-sm btn-outline"
                        onclick="selectInterfaceByIndex(${index})"
                        ${!iface.ipv4 ? "disabled" : ""}>
                    设为管理网卡
                </button>
            </td>
        </tr>`;
    }).join("");
}

async function selectInterfaceByIndex(index) {
    const iface = currentInterfaces[index];
    if (!iface) return;
    await selectInterface(iface.name, iface.ipv4, iface.netmask, false, iface.recommendation_reason);
}

async function selectInterfaceByName(name, auto = false) {
    const iface = currentInterfaces.find((item) => item.name === name);
    if (!iface) return;
    await selectInterface(iface.name, iface.ipv4, iface.netmask, auto, iface.recommendation_reason);
}

async function selectInterface(name, ip, netmask, auto = false, reason = "") {
    if (!ip || !netmask) {
        console.warn("该网卡没有有效的 IPv4 地址");
        return;
    }
    try {
        const data = await api("/api/interfaces/select", {
            method: "POST",
            body: JSON.stringify({ name, ip, netmask }),
        });
        const info = document.getElementById("selected-info");
        info.hidden = false;
        document.getElementById("selected-name").textContent = name;
        document.getElementById("selected-ip").textContent = ip;
        document.getElementById("selected-subnet").textContent = data.subnet;
        selectedSubnet = data.subnet || "";
        const subnetInput = document.getElementById("discover-subnet");
        if (subnetInput && !subnetInput.value.trim()) {
            subnetInput.value = selectedSubnet;
        }
        info.className = "info-bar " + (auto ? "success" : "info");
        info.title = auto && reason ? "自动识别原因：" + reason : "";
        // 不再自动触发采集，服务器启动时已自动完成
        // runAutoDiscoveryAndCollection();
    } catch (e) {
        console.error("选择网卡失败:", e.message);
    }
}

// ============================================================
// 5. 设备列表管理
// ============================================================

async function runAutoDiscoveryAndCollection() {
    if (!selectedSubnet || autoCollectInProgress) return;

    autoCollectInProgress = true;
    try {
        showStatus("batch-status", "已选择管理网卡，正在自动发现并采集设备信息...", "loading");
        await discoverDevices({ collectAfter: true, automatic: true });
    } finally {
        autoCollectInProgress = false;
    }
}

async function loadDevices() {
    try {
        const data = await api("/api/devices");
        currentDevices = data.devices;
        renderDevices(data.devices);
    } catch (e) {
        console.error("加载设备列表失败:", e);
    }
}

async function discoverDevices(options = {}) {
    const collectAfter = options.collectAfter !== false;
    const subnetInput = document.getElementById("discover-subnet");
    const subnet = subnetInput ? subnetInput.value.trim() : "";

    showStatus("batch-status", "正在扫描管理网段并识别 eNSP 设备...", "loading");
    setButtonLoading("btn-discover-devices", true);

    try {
        const data = await api("/api/discover-devices", {
            method: "POST",
            body: JSON.stringify({
                subnet: subnet || selectedSubnet || null,
                replace: true,
                ssh_identify: true,
            }),
        });

        if (data.suspicious) {
            showStatus("batch-status", data.message || "发现结果异常偏多，已保留当前设备列表", "error");
            return data;
        }

        currentDevices = data.devices || [];
        for (const result of Object.values(data.discovery_results || {})) {
            collectionResults[result.ip] = result;
        }

        renderDevices(currentDevices);
        renderDeviceCards(collectionResults);

        const sshOk = Object.values(data.discovery_results || {}).filter((r) => r.ssh_ok).length;
        const ignoredCount = (data.ignored || []).length;
        const ignoredText = ignoredCount ? `，已忽略 ${ignoredCount} 个未通过 Ping+SSH 的地址` : "";
        if (data.discovered.length && collectAfter) {
            showStatus(
                "batch-status",
                `发现 ${data.discovered.length} 台 Ping+SSH 通过的设备，正在自动采集设备信息${ignoredText}...`,
                "loading"
            );
            await collectAll({ automatic: true });
        } else {
            let message = data.discovered.length
                ? `发现 ${data.discovered.length} 台 Ping+SSH 通过的设备，${sshOk} 台完成 SSH 识别${ignoredText}，设备列表已同步`
                : data.replaced
                    ? `未发现 Ping+SSH 均通过的 eNSP 设备${ignoredText}，设备列表已同步为空。请检查 Cloud 绑定、管理 IP 和 SSH`
                    : `未发现 Ping+SSH 均通过的 eNSP 设备${ignoredText}，已保留当前设备列表。请检查 Cloud 绑定、管理 IP 和 SSH`;
            if (!data.discovered.length) {
                message += await getConsoleDiagnosticMessage();
            }
            showStatus("batch-status", message, data.discovered.length ? "success" : "error");
        }
        return data;
    } catch (e) {
        showStatus("batch-status", "发现设备失败: " + e.message, "error");
        return null;
    } finally {
        setButtonLoading("btn-discover-devices", false);
    }
}

function renderDevices(devices) {
    const tbody = document.getElementById("devices-tbody");
    if (!devices.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="table-empty">暂无设备，请扫描或手动添加</td></tr>';
        return;
    }

    tbody.innerHTML = devices
        .map((device, index) => {
            const result = collectionResults[device.ip] || {};
            const onlineStatus =
                result.online !== undefined
                    ? result.online
                        ? "在线"
                        : "离线"
                    : "未知";
            const sshStatus =
                result.ssh_ok !== undefined
                    ? result.ssh_ok
                        ? "已连接"
                        : "失败"
                    : "未采集";

            return `<tr>
                <td><strong>${escapeHtml(device.name)}</strong></td>
                <td>${escapeHtml(device.ip)}</td>
                <td>
                    <span class="badge ${
                        result.online
                            ? "badge-success"
                            : result.online === false
                                ? "badge-danger"
                                : "badge-info"
                    }">
                        ${onlineStatus}
                    </span>
                </td>
                <td>
                    <span class="badge ${
                        result.ssh_ok
                            ? "badge-success"
                            : result.ssh_ok === false
                                ? "badge-danger"
                                : "badge-info"
                    }">
                        ${sshStatus}
                    </span>
                </td>
                <td>
                    <div class="btn-group">
                        <button class="btn btn-sm btn-primary" onclick="showEditConnection(${index})">配置</button>
                    </div>
                </td>
            </tr>`;
        })
        .join("");
}

function showAddDevice() {
    editingDeviceIp = null;
    document.getElementById("device-form-title").textContent = "添加设备";
    document.getElementById("btn-add-device-submit").textContent = "确认添加";
    document.getElementById("add-name").value = "";
    document.getElementById("add-ip").value = "";
    document.getElementById("add-username").value = "admin";
    document.getElementById("add-password").value = "Huawei@123";
    document.getElementById("add-device-type").value = "huawei";
    document.getElementById("add-console-port").value = "";
    document.getElementById("add-device-form").hidden = false;
}

function showEditConnection(index) {
    const device = currentDevices[index];
    if (!device) return;

    editingDeviceIp = device.ip;
    document.getElementById("device-form-title").textContent = "编辑连接";
    document.getElementById("btn-add-device-submit").textContent = "保存配置";
    document.getElementById("add-name").value = device.name || "";
    document.getElementById("add-ip").value = device.ip || "";
    document.getElementById("add-username").value = device.username || "admin";
    document.getElementById("add-password").value = device.password || "Huawei@123";
    document.getElementById("add-device-type").value = device.device_type || "huawei";
    document.getElementById("add-console-port").value = device.console_port || "";
    const form = document.getElementById("add-device-form");
    form.hidden = false;
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// showConfigureDevice 保留为 showEditConnection 的别名
function showConfigureDevice(index) {
    showEditConnection(index);
}

function hideAddDevice() {
    document.getElementById("add-device-form").hidden = true;
    editingDeviceIp = null;
}

async function saveDeviceForm() {
    const name = document.getElementById("add-name").value.trim();
    const ip = document.getElementById("add-ip").value.trim();
    const username = document.getElementById("add-username").value.trim();
    const password = document.getElementById("add-password").value;
    const device_type = document.getElementById("add-device-type").value;
    const consolePortRaw = document.getElementById("add-console-port").value.trim();
    const console_port = consolePortRaw ? parseInt(consolePortRaw, 10) : undefined;

    if (!name || !ip) {
        console.warn("设备名称和 IP 不能为空");
        return;
    }

    setButtonLoading("btn-add-device-submit", true);
    try {
        const wasEditing = Boolean(editingDeviceIp);
        const path = editingDeviceIp
            ? `/api/devices/${encodeURIComponent(editingDeviceIp)}`
            : "/api/devices";
        const method = editingDeviceIp ? "PUT" : "POST";
        const body = { name, ip, username, password, device_type };
        if (console_port !== undefined) body.console_port = console_port;
        await api(path, {
            method,
            body: JSON.stringify(body),
        });
        hideAddDevice();
        await loadDevices();
        showStatus("batch-status", wasEditing ? "设备配置已保存" : "设备已添加", "success");
    } catch (e) {
        showStatus("batch-status", "保存设备配置失败: " + e.message, "error");
    } finally {
        setButtonLoading("btn-add-device-submit", false);
    }
}

async function addDevice() {
    await saveDeviceForm();
}

async function confirmDelete(ip, name) {
    try {
        await api(`/api/devices/${ip}`, { method: "DELETE" });
        delete collectionResults[ip];
        await loadDevices();
    } catch (e) {
        console.error("删除失败:", e.message);
    }
}

async function pingDevice(ip) {
    try {
        const data = await api("/api/ping", {
            method: "POST",
            body: JSON.stringify({ ip }),
        });
        console.log("Ping 结果:", ip, data.message);

        // 更新采集结果中的在线状态
        if (!collectionResults[ip]) {
            collectionResults[ip] = { ip, commands: {}, info: {} };
        }
        collectionResults[ip].online = data.online;
        renderDevices(currentDevices);
    } catch (e) {
        console.error("Ping 失败:", e.message);
    }
}

// ============================================================
// 6. 信息采集
// ============================================================

async function collectDevice(ip) {
    showStatus("batch-status", `正在采集 ${ip} 的信息...`, "loading");
    try {
        const data = await api(`/api/collect/${ip}`, { method: "POST" });
        collectionResults[ip] = data.result;
        renderDevices(currentDevices);
        renderDeviceCards(collectionResults);
        hideStatus("batch-status");

        // 自动触发单设备能力识别
        if (data.result && data.result.ssh_ok) {
            probeSingleCapability(ip);
        }
    } catch (e) {
        showStatus("batch-status", `采集 ${ip} 失败: ${e.message}`, "error");
    }
}

async function collectAll(options = {}) {
    const deviceCount = currentDevices.length;
    if (!deviceCount) {
        showStatus("batch-status", "设备列表为空，无法自动采集", "error");
        return;
    }

    showStatus("batch-status", `正在自动采集 ${deviceCount} 台设备信息...`, "loading");
    const collectButton = document.getElementById("btn-collect-all");
    if (collectButton) setButtonLoading("btn-collect-all", true);

    try {
        const data = await api("/api/collect-all", { method: "POST" });
        if (data.devices) {
            currentDevices = data.devices;
        }
        for (const result of data.results) {
            collectionResults[result.ip] = result;
        }
        renderDevices(currentDevices);
        renderDeviceCards(collectionResults);

        const success = data.results.filter((r) => r.ssh_ok).length;
        const lldpText = data.lldp_added && data.lldp_added.length
            ? `，LLDP 新增 ${data.lldp_added.length} 台`
            : "";
        showStatus(
            "batch-status",
            `自动采集完成: ${success}/${data.results.length} 台设备 SSH 连接成功${lldpText}，正在进行能力识别...`,
            success > 0 ? "success" : "error"
        );
        if (options.automatic) {
            await loadTopology({ silent: true });
        }

        // 自动触发能力识别
        if (success > 0) {
            await _autoProbeCapabilities();
        }
    } catch (e) {
        showStatus("batch-status", "自动采集失败: " + e.message, "error");
    } finally {
        if (collectButton) setButtonLoading("btn-collect-all", false);
    }
}

async function loadCollectionResults() {
    try {
        const data = await api("/api/collection-results");
        collectionResults = data.results;
        renderDevices(currentDevices);
        renderDeviceCards(collectionResults);
    } catch (e) {
        console.error("加载采集结果失败:", e);
    }
}

async function loadCapabilityResults() {
    try {
        const data = await api("/api/capability-results");
        capabilityResults = data.results || {};
        if (Object.keys(capabilityResults).length) {
            renderCapabilityResults(Object.values(capabilityResults));
        }
        // 始终更新配置下发设备选择（以显示/隐藏能力识别提示）
        updateConfigDeviceSelect();
    } catch (e) {
        console.error("加载能力识别结果失败:", e);
    }
}

// ============================================================
// 7. 设备信息展示（卡片）
// ============================================================

const PROTOCOL_NAMES = {
    ospf: "OSPF", rip: "RIP", bgp: "BGP", isis: "IS-IS",
    dhcp: "DHCP", nat: "NAT", ipsec: "IPSec",
    ntp: "NTP", snmp: "SNMP", acl: "ACL", static: "静态路由",
};

function renderDeviceCards(results) {
    const container = document.getElementById("device-cards");
    const badge = document.getElementById("device-count-badge");
    const ips = Object.keys(results);

    if (!ips.length) {
        container.innerHTML =
            '<p class="empty-state">暂无采集数据，请先执行设备采集</p>';
        badge.hidden = true;
        return;
    }

    const deviceCount = ips.length;
    badge.hidden = false;
    badge.textContent = `${deviceCount} 台设备`;

    container.innerHTML = ips
        .map((ip) => {
            const result = results[ip];
            const info = result.info || {};
            const commands = result.commands || {};

            const deviceName = info.hostname || result.name || ip;
            const model = info.model || "未知型号";
            const version = info.version || "-";
            const cpu = info.cpu_usage || "-";
            const memory = info.memory_usage || "-";

            // 协议配置状态标签
            const protocols = info.protocols || [];
            const protocolBadges = protocols.length
                ? protocols
                    .map(
                        (p) =>
                            `<span class="badge badge-protocol" title="${escapeHtml(PROTOCOL_NAMES[p] || p)} 已配置">${escapeHtml(PROTOCOL_NAMES[p] || p)} 已配置</span>`
                    )
                    .join("")
                : '<span class="badge badge-muted">未检测到协议配置</span>';

            // 配置段下拉详情
            const configSections = info.config_sections || {};
            const configSectionEntries = Object.entries(configSections);
            const configSectionsHtml = configSectionEntries.length
                ? `<div class="config-sections">
                        <div class="config-sections-title" onclick="toggleConfigSections(this)">
                            <span class="config-sections-arrow">&#9654;</span>
                            <span>协议配置详情（${configSectionEntries.length} 项）</span>
                        </div>
                        <div class="config-sections-body collapsible">
                            ${configSectionEntries
                                .map(([key, sec]) => {
                                    return `<div class="config-block">
                                        <h4 class="config-block-title">${escapeHtml(sec.name || key)}</h4>
                                        <pre class="config-block-pre">${escapeHtml(sec.config || "")}</pre>
                                    </div>`;
                                })
                                .join("")}
                        </div>
                    </div>`
                : "";

            const commandSections = Object.entries(commands)
                .map(([cmd, cmdData]) => {
                    const status = cmdData.success
                        ? '<span class="badge badge-success">成功</span>'
                        : '<span class="badge badge-danger">失败</span>';
                    const output = cmdData.success
                        ? `<pre>${escapeHtml(cmdData.output)}</pre>`
                        : `<span class="error-text">执行失败: ${escapeHtml(cmdData.error || "未知错误")}</span>`;

                    return `
                        <div class="cmd-section">
                            <h4>${escapeHtml(cmd)} ${status}</h4>
                            ${output}
                        </div>`;
                })
                .join("");

            return `
                <div class="device-card">
                    <div class="device-card-header" onclick="toggleCard(this)">
                        <h3>${escapeHtml(deviceName)} (${escapeHtml(ip)})</h3>
                        <div class="meta">
                            <span>${escapeHtml(model)}</span>
                            <span>v${escapeHtml(version)}</span>
                            <span>CPU: ${escapeHtml(cpu)}</span>
                            <span>MEM: ${escapeHtml(memory)}</span>
                            <span class="badge ${result.online ? "badge-success" : "badge-danger"}">
                                ${result.online ? "在线" : "离线"}
                            </span>
                            <span class="badge ${result.ssh_ok ? "badge-success" : "badge-danger"}">
                                ${result.ssh_ok ? "SSH 已连接" : "SSH " + (result.ssh_error || "失败")}
                            </span>
                            ${protocolBadges}
                        </div>
                    </div>
                    <div class="device-card-body collapsible">
                        ${configSectionsHtml}
                        ${commandSections}
                    </div>
                </div>`;
        })
        .join("");
}

function toggleCard(header) {
    const body = header.nextElementSibling;
    body.classList.toggle("open");
    header.classList.toggle("collapsed");
}

function toggleConfigSections(titleEl) {
    const body = titleEl.nextElementSibling;
    body.classList.toggle("open");
    const arrow = titleEl.querySelector(".config-sections-arrow");
    if (arrow) {
        arrow.innerHTML = body.classList.contains("open") ? "&#9660;" : "&#9654;";
    }
}

// ============================================================
// 8. 拓扑关系
// ============================================================

async function loadTopology(options = {}) {
    if (!options.silent) {
        showStatus("batch-status", "正在加载拓扑信息...", "loading");
    }
    setButtonLoading("btn-refresh-topology", true);

    try {
        const data = await api("/api/topology");
        renderTopology(data.topology.links);
        if (!options.silent) {
            hideStatus("batch-status");
        }
    } catch (e) {
        if (!options.silent) {
            showStatus("batch-status", "加载拓扑失败: " + e.message, "error");
        }
    } finally {
        setButtonLoading("btn-refresh-topology", false);
    }
}

function renderTopology(links) {
    const tbody = document.getElementById("topology-tbody");

    if (!links.length) {
        tbody.innerHTML =
            '<tr><td colspan="6" class="table-empty">暂无拓扑数据，请先采集设备 LLDP 信息</td></tr>';
        return;
    }

    tbody.innerHTML = links
        .map(
            (link) => `
            <tr>
                <td><strong>${escapeHtml(link.source)}</strong></td>
                <td>${escapeHtml(link.source_ip)}</td>
                <td><code>${escapeHtml(link.local_interface)}</code></td>
                <td><strong>${escapeHtml(link.target)}</strong></td>
                <td>${escapeHtml(link.target_ip || "未知")}</td>
                <td><code>${escapeHtml(link.remote_interface)}</code></td>
            </tr>`
        )
        .join("");
}

// ============================================================
// 9. 协议配置
// ============================================================

// 协议配置全局状态
let protocolTemplates = {};   // 从后端 /api/config-templates 拉取（单一数据源，不再前端硬编码表单）
let protocolConfigState = {
    deviceIp: null,
    deviceIndex: null,
    protocols: [],
    configSections: {},
    configSource: "",
    selectedProtocol: null,
    configMode: "form",
};

/**
 * 显示协议配置模态框
 */
function showProtocolConfig(index) {
    const device = currentDevices[index];
    if (!device) return;

    protocolConfigState.deviceIp = device.ip;
    protocolConfigState.deviceIndex = index;
    protocolConfigState.selectedProtocol = null;

    // 设置设备名称
    document.getElementById("protocol-device-name").textContent = `${device.name} (${device.ip})`;

    // 显示模态框
    document.getElementById("protocol-config-modal").classList.remove("hidden");

    // 加载协议配置
    loadProtocolConfig(device.ip);
}

/**
 * 关闭协议配置模态框
 */
function closeProtocolConfig() {
    document.getElementById("protocol-config-modal").classList.add("hidden");
    protocolConfigState = {
        deviceIp: null,
        deviceIndex: null,
        protocols: [],
        configSections: {},
        configSource: "",
        selectedProtocol: null,
        configMode: "form",
    };
}

/**
 * 加载设备协议配置
 */
async function loadProtocolConfig(ip) {
    try {
        const [data, tplData] = await Promise.all([
            api(`/api/devices/${encodeURIComponent(ip)}/protocols`),
            api("/api/config-templates"),
        ]);
        protocolConfigState.protocols = data.protocols || [];
        protocolConfigState.configSections = data.config_sections || {};
        protocolConfigState.configSource = data.config_source || "cache";
        protocolTemplates = (tplData && tplData.templates) || {};

        // 渲染已检测到的协议
        renderDetectedProtocols();

        // 渲染协议标签页
        renderProtocolTabs();
        if (protocolConfigState.configSource === "backup") {
            showProtocolStatus(
                `实时获取失败，当前展示历史备份 ${data.config_backup_file || ""}`,
                "warning"
            );
        }
    } catch (e) {
        console.error("加载协议配置失败:", e);
        showProtocolStatus("加载协议配置失败: " + e.message, "error");
    }
}

/**
 * 渲染已检测到的协议状态
 */
function renderDetectedProtocols() {
    const container = document.getElementById("detected-protocols");
    const protocols = protocolConfigState.protocols;

    if (!protocols.length) {
        container.innerHTML = '<span class="badge badge-muted">未检测到协议配置</span>';
        return;
    }

    container.innerHTML = protocols
        .map(p => `<span class="badge badge-success">${escapeHtml((protocolTemplates[p] && protocolTemplates[p].name) || PROTOCOL_NAMES[p] || p)}</span>`)
        .join(" ");
}

/**
 * 渲染协议选择标签页
 */
function renderProtocolTabs() {
    const container = document.getElementById("protocol-tabs");
    const allProtocols = Object.keys(protocolTemplates);
    const configuredProtocols = protocolConfigState.protocols;

    container.innerHTML = allProtocols
        .map(p => {
            const isConfigured = configuredProtocols.includes(p);
            const form = protocolTemplates[p];
            const isActive = protocolConfigState.selectedProtocol === p;
            return `<button class="protocol-tab ${isActive ? "active" : ""} ${isConfigured ? "configured" : ""}"
                        data-protocol="${p}"
                        onclick="selectProtocol('${p}')">
                        <span class="protocol-tab-icon">${form.icon || "CF"}</span>
                        <span class="protocol-tab-name">${escapeHtml(form.name || p)}</span>
                        ${isConfigured ? '<span class="protocol-tab-badge">&#x2713;</span>' : ""}
                    </button>`;
        })
        .join("");
}

/**
 * 选择协议
 */
function selectProtocol(protocol) {
    protocolConfigState.selectedProtocol = protocol;

    // 更新标签页状态
    document.querySelectorAll(".protocol-tab").forEach(tab => {
        tab.classList.toggle("active", tab.dataset.protocol === protocol);
    });

    // 渲染协议表单
    renderProtocolForm(protocol);

    // 显示当前配置预览
    showCurrentConfig(protocol);
}

/**
 * 渲染协议配置表单
 */
function renderProtocolForm(protocol) {
    const tpl = protocolTemplates[protocol];
    if (!tpl) return;

    const container = document.getElementById("protocol-form-config");
    const currentConfig = protocolConfigState.configSections[protocol];
    const existingParams = currentConfig ? parseConfigToParams(protocol, currentConfig.config || "") : {};

    let html = `<div class="protocol-form">
        <h4 class="protocol-form-title">${tpl.icon || ""} ${escapeHtml(tpl.name || protocol)}</h4>
        <div class="protocol-form-fields">`;

    // 复用通用表单渲染器 renderFormField（单一渲染器，消除手写 select/checkbox/input 分叉）
    for (const field of (tpl.fields || [])) {
        const preset = existingParams[field.name];
        const f = preset !== undefined ? { ...field, default: preset } : field;
        html += renderFormField(f, null);
    }

    html += `</div></div>`;
    container.innerHTML = html;
}

/**
 * 简单解析现有配置到参数（基础实现）
 */
function parseConfigToParams(protocol, config) {
    const params = {};
    const lines = config.split("\n");

    // 基础解析逻辑
    for (const line of lines) {
        const trimmed = line.trim();
        if (protocol === "ospf" && trimmed.startsWith("ospf ")) {
            params.process_id = trimmed.split(/\s+/)[1];
        } else if (protocol === "bgp" && trimmed.startsWith("bgp ")) {
            params.local_as = trimmed.split(/\s+/)[1];
        } else if (protocol === "rip" && trimmed === "rip") {
            // RIP 配置开始
        }
    }

    return params;
}

/**
 * 切换配置模式
 */
function switchConfigMode(mode) {
    protocolConfigState.configMode = mode;

    // 更新按钮状态
    document.querySelectorAll(".config-mode-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.mode === mode);
    });

    // 显示/隐藏配置区域
    document.getElementById("protocol-form-config").classList.toggle("hidden", mode !== "form");
    document.getElementById("protocol-command-config").classList.toggle("hidden", mode !== "command");
}

/**
 * 显示当前配置预览
 */
function showCurrentConfig(protocol) {
    const preview = document.getElementById("current-config-preview");
    const content = preview.querySelector(".config-preview-content");
    const configSections = protocolConfigState.configSections;
    const protocolConfig = configSections[protocol];

    if (protocolConfig && protocolConfig.config) {
        content.textContent = protocolConfig.config;
        preview.classList.remove("hidden");
    } else {
        preview.classList.add("hidden");
    }
}

/**
 * 应用协议配置
 */
async function applyProtocolConfig() {
    const protocol = protocolConfigState.selectedProtocol;
    const ip = protocolConfigState.deviceIp;

    if (!protocol || !ip) {
        showProtocolStatus("请先选择要配置的协议", "warning");
        return;
    }

    const mode = protocolConfigState.configMode;
    let requestData = { mode: mode };

    if (mode === "form") {
        // 收集表单数据
        const form = document.getElementById("protocol-form-config");
        const inputs = form.querySelectorAll(".protocol-input, .protocol-checkbox");
        const params = {};

        inputs.forEach(input => {
            if (input.type === "checkbox") {
                params[input.name] = input.checked;
            } else {
                params[input.name] = input.value;
            }
        });

        requestData.params = params;
    } else {
        // 收集命令行数据
        const commandsText = document.getElementById("protocol-commands").value.trim();
        if (!commandsText) {
            showProtocolStatus("请输入配置命令", "warning");
            return;
        }
        requestData.commands = commandsText.split("\n").filter(cmd => cmd.trim());
    }

    // 确认操作
    const confirmed = await showConfirm(
        "确认配置",
        `确定要将 ${PROTOCOL_NAMES[protocol] || protocol} 配置应用到设备 ${ip} 吗？`
    );
    if (!confirmed) return;

    // 执行配置
    setButtonLoading("btn-apply-protocol", true);
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/protocols/${protocol}`, {
            method: "POST",
            body: JSON.stringify(requestData),
        });

        showProtocolStatus(`配置已成功应用到设备`, "success");

        // 重新加载协议配置
        await loadProtocolConfig(ip);

        // 刷新采集结果
        await loadCollectionResults();
    } catch (e) {
        showProtocolStatus("配置应用失败: " + e.message, "error");
    } finally {
        setButtonLoading("btn-apply-protocol", false);
    }
}

/**
 * 删除协议配置
 */
async function deleteProtocolConfig() {
    const protocol = protocolConfigState.selectedProtocol;
    const ip = protocolConfigState.deviceIp;

    if (!protocol || !ip) {
        showProtocolStatus("请先选择要删除的协议", "warning");
        return;
    }

    // 确认操作
    const confirmed = await showConfirm(
        "确认删除",
        `确定要删除设备 ${ip} 上的 ${PROTOCOL_NAMES[protocol] || protocol} 配置吗？`
    );
    if (!confirmed) return;

    // 执行删除
    setButtonLoading("btn-delete-protocol", true);
    try {
        await api(`/api/devices/${encodeURIComponent(ip)}/protocols/${protocol}`, {
            method: "DELETE",
        });

        showProtocolStatus(`${PROTOCOL_NAMES[protocol] || protocol} 配置已删除`, "success");

        // 重新加载协议配置
        await loadProtocolConfig(ip);

        // 刷新采集结果
        await loadCollectionResults();
    } catch (e) {
        showProtocolStatus("删除配置失败: " + e.message, "error");
    } finally {
        setButtonLoading("btn-delete-protocol", false);
    }
}

/**
 * 显示协议配置状态信息
 */
function showProtocolStatus(message, type = "info") {
    // 在模态框内显示状态
    const modal = document.getElementById("protocol-config-modal");
    let statusBar = modal.querySelector(".protocol-status-message");

    if (!statusBar) {
        statusBar = document.createElement("div");
        statusBar.className = "protocol-status-message";
        modal.querySelector(".modal-body").prepend(statusBar);
    }

    statusBar.className = `protocol-status-message status-${type}`;
    statusBar.textContent = message;
    statusBar.style.display = "block";

    // 3秒后自动隐藏
    setTimeout(() => {
        statusBar.style.display = "none";
    }, 3000);
}

/**
 * 清空命令编辑器
 */
function clearCommandEditor() {
    document.getElementById("protocol-commands").value = "";
}

/**
 * 显示确认对话框（返回 Promise）
 */
function showConfirm(title, message) {
    return new Promise((resolve) => {
        const modal = document.getElementById("custom-modal");
        const modalTitle = document.getElementById("modal-title");
        const modalMessage = document.getElementById("modal-message");
        const modalOk = document.getElementById("modal-ok");
        const modalCancel = document.getElementById("modal-cancel");

        modalTitle.textContent = title;
        modalMessage.textContent = message;
        modal.classList.remove("hidden");

        const handleOk = () => {
            modal.classList.add("hidden");
            modalOk.removeEventListener("click", handleOk);
            modalCancel.removeEventListener("click", handleCancel);
            resolve(true);
        };

        const handleCancel = () => {
            modal.classList.add("hidden");
            modalOk.removeEventListener("click", handleOk);
            modalCancel.removeEventListener("click", handleCancel);
            resolve(false);
        };

        modalOk.addEventListener("click", handleOk);
        modalCancel.addEventListener("click", handleCancel);
    });
}

// 切换配置预览展开/收起
document.addEventListener("click", function(e) {
    if (e.target.closest(".config-preview-header")) {
        const preview = e.target.closest(".config-preview");
        const content = preview.querySelector(".config-preview-content");
        const arrow = preview.querySelector(".config-preview-arrow");
        content.classList.toggle("open");
        arrow.innerHTML = content.classList.contains("open") ? "&#9660;" : "&#9654;";
    }
});

// ============================================================
// 10. 页面初始化
// ============================================================

// ============================================================
// 11. 设备能力识别
// ============================================================

let capabilityResults = {};

const CAPABILITY_NAMES = {
    static_route: "静态路由", ospf: "OSPF", isis: "IS-IS", bgp: "BGP", rip: "RIP",
    routing_table: "路由表", vlan: "VLAN", access_port: "Access 接口",
    trunk_port: "Trunk 接口", vlanif: "VLANIF", stp: "STP/RSTP/MSTP",
    eth_trunk: "Eth-Trunk", lldp: "LLDP", interface_ip: "接口 IP",
    acl: "ACL", dhcp: "DHCP", nat: "NAT", vrrp: "VRRP",
    snmp: "SNMP", ssh: "SSH/STelnet", telnet: "Telnet",
    // 安全/VPN 功能
    ipsec: "IPsec VPN", ike: "IKE/IKEv2", gre: "GRE 隧道", vpn_instance: "VPN 实例/VRF",
    // 扩展协议（完整协议清单）
    ntp: "NTP", l2tp: "L2TP VPN", pppoe: "PPPoE", ipv6: "IPv6 基础",
    mpls: "MPLS", mpls_ldp: "MPLS LDP", igmp: "IGMP", pim: "PIM",
    igmp_snooping: "IGMP Snooping",
};

// 证据来源徽标：evidence 字段 → 中文标签
function evidenceBadge(cap) {
    if (!cap || !cap.evidence) return "";
    const ev = String(cap.evidence);
    let tag = "";
    if (ev.includes("helptree")) tag = "?树实测";
    else if (ev.includes("matrix")) tag = "镜像矩阵";
    if (!tag) return "";
    return `<span class="cap-badge evidence" title="判定依据: ${escapeHtml(ev)}${cap.note ? ' | ' + escapeHtml(cap.note) : ''}">${tag}</span>`;
}

let protocolTreeResults = {};

/** ? 命令树协议发现（零副作用：只发 ? 和 display 类命令） */
async function discoverProtocolTree(ip) {
    const container = document.getElementById(`ptree-${ip}`);
    if (!container) return;
    container.classList.add("open");
    container.innerHTML = '<p class="empty-state">正在采集 ? 命令树（display ? / 系统视图 / 接口视图）...</p>';
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/protocol-tree`);
        protocolTreeResults[ip] = data;
        container.innerHTML = renderProtocolTree(data);
    } catch (e) {
        container.innerHTML = `<div class="info-bar error">命令树发现失败: ${escapeHtml(e.message)}</div>`;
    }
}

function renderProtocolTree(data) {
    if (!data || data.success === false) {
        return '<div class="info-bar error">命令树发现失败</div>';
    }
    const views = data.views || {};
    const viewLines = Object.entries(views).map(([scope, kws]) => {
        const label = { display: "display ?", system: "系统视图 ?", interface: "接口视图 ?" }[scope] || scope;
        return `<div class="ptree-view"><strong>${escapeHtml(label)}</strong>（${kws.length} 个关键字）: `
            + `<span class="ptree-kws">${escapeHtml(kws.slice(0, 60).join("、"))}${kws.length > 60 ? " …" : ""}</span></div>`;
    }).join("");

    const supported = Object.entries(data.protocols || {}).filter(([, p]) => p.supported);
    const protocolLine = supported.length
        ? `实得支持协议 ${supported.length} 项: ` + supported.map(([k, p]) =>
            `<span class="ptree-proto" title="${escapeHtml(p.evidence || "")}">${escapeHtml(p.name || k)}</span>`).join("")
        : "未发现支持协议";

    const meta = [];
    if (data.model) meta.push(`型号 ${data.model}`);
    if (data.image) meta.push(`镜像 ${data.image}`);
    if (data.interface) meta.push(`接口视图 ${data.interface}`);

    return `<div class="ptree-box">
        <div class="info-bar">${meta.length ? escapeHtml(meta.join(" | ")) + " — " : ""}支持协议 ${supported.length} 项</div>
        <div class="ptree-protocols">${protocolLine}</div>
        ${viewLines}
    </div>`;
}

const ROLE_NAMES = {
    router: "路由器", switch: "交换机", l3_switch: "三层交换机", unknown: "未知设备",
};

async function probeAllCapabilities() {
    setButtonLoading("btn-probe-all", true);
    try {
        const data = await api("/api/devices/capabilities-all", { method: "POST" });
        capabilityResults = {};
        for (const r of data.results) {
            capabilityResults[r.ip] = r;
        }
        renderCapabilityResults(data.results);
        updateConfigDeviceSelect();
        analyzeProtocolRoles(true);
    } catch (e) {
        console.error("能力识别失败:", e.message);
    } finally {
        setButtonLoading("btn-probe-all", false);
    }
}

/** 自动批量能力识别（采集完成后调用，不操作按钮状态） */
async function _autoProbeCapabilities() {
    try {
        const btn = document.getElementById("btn-probe-all");
        if (btn) setButtonLoading("btn-probe-all", true);
        const data = await api("/api/devices/capabilities-all", { method: "POST" });
        capabilityResults = {};
        for (const r of data.results) {
            capabilityResults[r.ip] = r;
        }
        renderCapabilityResults(data.results);
        updateConfigDeviceSelect();
        analyzeProtocolRoles(true);
        showStatus(
            "batch-status",
            `采集及能力识别完成: ${data.results.filter(r => r.ssh_status).length}/${data.results.length} 台设备识别成功`,
            "success"
        );
    } catch (e) {
        console.error("自动能力识别失败:", e.message);
    } finally {
        const btn = document.getElementById("btn-probe-all");
        if (btn) setButtonLoading("btn-probe-all", false);
    }
}

async function probeSingleCapability(ip) {
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/capabilities`);
        capabilityResults[ip] = data;
        renderCapabilityResults(Object.values(capabilityResults));
        updateConfigDeviceSelect();
    } catch (e) {
        console.error(`能力识别 ${ip} 失败:`, e.message);
    }
}

function renderCapabilityResults(results) {
    const container = document.getElementById("capability-results");
    if (!results || !results.length) {
        container.innerHTML = '<p class="empty-state">暂无能力识别数据</p>';
        return;
    }

    container.innerHTML = results.map(r => {
        const role = r.device_role || "unknown";
        const caps = r.capabilities || {};
        const capEntries = Object.entries(caps);

        const supportedCount = capEntries.filter(([, c]) => c.supported).length;
        const configuredCount = capEntries.filter(([, c]) => c.configured).length;
        const runningCount = capEntries.filter(([, c]) => c.running).length;

        const capItems = capEntries.map(([key, cap]) => {
            let badges = "";
            if (cap.supported) {
                badges += '<span class="cap-badge supported">支持</span>';
                if (cap.configured) badges += '<span class="cap-badge configured">已配置</span>';
                if (cap.running) badges += '<span class="cap-badge running">运行中</span>';
            } else {
                badges += '<span class="cap-badge unsupported">不支持</span>';
            }
            badges += evidenceBadge(cap);
            return `<div class="capability-item" title="${escapeHtml((cap.display_cmd || '') + (cap.note ? ' | ' + cap.note : ''))}">
                <span class="capability-name">${escapeHtml(CAPABILITY_NAMES[key] || key)}</span>
                <span class="capability-badges">${badges}</span>
            </div>`;
        }).join("");

        const errors = (r.errors || []).length
            ? `<div class="info-bar error" style="margin-top:8px">错误: ${escapeHtml(r.errors.join("; "))}</div>`
            : "";

        // 镜像矩阵信息徽标
        const matrix = r.matrix || {};
        const matrixBadge = matrix.family
            ? `<span class="cap-badge matrix" title="eNSP 镜像能力矩阵匹配: ${escapeHtml(matrix.family_label || matrix.family)}${matrix.image ? ' (' + escapeHtml(matrix.image) + ')' : ''}">矩阵 ${escapeHtml(matrix.family_label || matrix.family)}</span>`
            : (matrix.image ? `<span class="cap-badge matrix unknown-matrix" title="镜像 ${escapeHtml(matrix.image)} 未收录矩阵，全部以实探为准">矩阵未收录</span>` : "");

        const treeBtn = r.ssh_status
            ? `<button class="btn btn-sm btn-outline ptree-btn" onclick="event.stopPropagation(); discoverProtocolTree('${escapeHtml(r.ip)}')">? 命令树协议发现</button>`
            : "";
        const treeContainer = r.ssh_status
            ? `<div id="ptree-${escapeHtml(r.ip)}" class="ptree-container collapsible"></div>`
            : "";

        return `<div class="capability-card">
            <div class="capability-card-header" onclick="toggleCard(this)">
                <h3>${escapeHtml(r.hostname || r.ip)} (${escapeHtml(r.ip)})</h3>
                <div class="meta">
                    <span class="device-role-badge ${role}">${escapeHtml(ROLE_NAMES[role] || role)}</span>
                    <span class="badge ${r.ssh_status ? 'badge-success' : 'badge-danger'}">${r.ssh_status ? 'SSH OK' : 'SSH 失败'}</span>
                    <span class="badge badge-info">支持 ${supportedCount}</span>
                    <span class="badge badge-success">已配置 ${configuredCount}</span>
                    <span class="badge" style="background:rgba(155,89,182,0.15);color:#9b59b6">运行中 ${runningCount}</span>
                    ${matrixBadge}
                </div>
            </div>
            <div class="capability-card-body collapsible">
                <div class="capability-grid">${capItems}</div>
                ${treeBtn}${treeContainer}
                ${errors}
            </div>
        </div>`;
    }).join("");
}

// ============================================================
// 11.5 协议角色总览 + 填空式配置
// ============================================================

let protocolRoleResults = {};

/** 分析所有已识别设备的协议角色（纯后端解析，零 SSH） */
async function analyzeProtocolRoles(silent) {
    const btn = document.getElementById("btn-analyze-roles");
    if (!silent && btn) setButtonLoading("btn-analyze-roles", true);
    try {
        const data = await api("/api/protocol-roles-all", { method: "POST" });
        protocolRoleResults = {};
        for (const r of data.results || []) protocolRoleResults[r.ip] = r;
        renderProtocolRoles(data.results || []);
        if (!silent && data.count === 0) {
            showStatus("batch-status", "暂无已识别能力的设备，请先执行能力识别", "warning");
        }
    } catch (e) {
        if (!silent) console.error("协议角色分析失败:", e.message);
    } finally {
        if (!silent && btn) setButtonLoading("btn-analyze-roles", false);
    }
}

const PROTOCOL_ROLE_STATUS = {
    running: '<span class="cap-badge running">运行中</span>',
    configured: '<span class="cap-badge configured">已配置</span>',
    supported: '<span class="cap-badge supported">未配置</span>',
};

function renderProtocolRoles(results) {
    const container = document.getElementById("protocol-roles-results");
    if (!container) return;
    if (!results || !results.length) {
        container.innerHTML = '<p class="empty-state">暂无角色分析数据，请先执行能力识别，再点击"分析协议角色"</p>';
        return;
    }

    container.innerHTML = results.map(r => {
        const entries = Object.entries(r.protocols || {});
        const runningCount = entries.filter(([, p]) => p.status === "running").length;
        const fillableCount = entries.filter(([, p]) => p.config_key).length;

        const rows = entries.map(([key, p]) => {
            const statusBadge = PROTOCOL_ROLE_STATUS[p.status]
                || '<span class="cap-badge unsupported">未知</span>';
            const roleChips = (p.roles || []).map(rl =>
                `<span class="role-chip" title="${escapeHtml((rl.detail || '') + (rl.interface ? ' @' + rl.interface : ''))}">${escapeHtml(rl.role)}${rl.interface ? `<small>@${escapeHtml(rl.interface)}</small>` : ''}</span>`
            ).join("");
            const fillBtn = p.config_key
                ? `<button class="btn btn-sm btn-outline role-fill-btn" onclick="fillInConfig('${escapeHtml(r.ip)}', '${escapeHtml(p.config_key)}', '${escapeHtml(key)}')">填空配置</button>`
                : "";
            return `<div class="role-row">
                <span class="role-proto">${escapeHtml(p.name)}</span>
                <span class="role-chips">${roleChips || '<span class="role-chip empty">未配置，点右侧开始</span>'}</span>
                ${statusBadge}${fillBtn}
            </div>`;
        }).join("");

        const role = r.device_role || "unknown";
        return `<div class="capability-card">
            <div class="capability-card-header" onclick="toggleCard(this)">
                <h3>${escapeHtml(r.hostname || r.ip)} (${escapeHtml(r.ip)})</h3>
                <div class="meta">
                    <span class="device-role-badge ${role}">${escapeHtml(ROLE_NAMES[role] || role)}</span>
                    <span class="badge" style="background:rgba(155,89,182,0.15);color:#9b59b6">运行中 ${runningCount}</span>
                    <span class="badge badge-info">可填空配置 ${fillableCount}</span>
                </div>
            </div>
            <div class="capability-card-body collapsible">
                <div class="role-table">${rows}</div>
            </div>
        </div>`;
    }).join("");
}

/**
 * 填空式傻瓜配置：从协议角色一键跳到对应模板弹窗，
 * 按设备现状预填可推导的字段（接口/IP/网段/ACL 编号等），用户补齐空格即可。
 */
async function fillInConfig(ip, configKey, protocolKey) {
    showSection("section-config-deploy");

    const select = document.getElementById("config-device-select");
    if (!select) return;
    select.value = ip;
    if (select.value !== ip) {
        showStatus("batch-status", `设备 ${ip} 不在设备列表中，请先添加`, "error");
        return;
    }
    await onConfigDeviceSelected();

    const tpl = configDeployState.templates[configKey];
    if (!tpl) {
        showStatus("batch-status", `该设备角色不支持模板「${configKey}」`, "error");
        return;
    }
    selectConfigTemplate(configKey);

    const info = (protocolRoleResults[ip] || {}).protocols || {};
    const proto = info[protocolKey] || {};
    const presets = proto.presets || {};
    const hints = proto.preset_hints || [];

    let filled = 0;
    document.querySelectorAll("#config-modal-form-fields [name]").forEach(el => {
        const v = presets[el.name];
        if (v === undefined || v === "") return;
        if (el.type === "checkbox") {
            el.checked = String(v).toLowerCase() === "true";
        } else {
            el.value = v;
        }
        filled++;
    });

    if (filled) {
        const titleEl = document.getElementById("config-modal-title");
        if (titleEl) titleEl.textContent += `（已预填 ${filled} 项）`;
        if (hints.length) {
            const note = document.createElement("div");
            note.className = "info-bar info";
            note.style.marginBottom = "8px";
            note.textContent = "预填说明: " + hints.join("；") + "。请补齐其余空格后生成预览。";
            const formEl = document.getElementById("config-modal-form-fields");
            if (formEl) formEl.prepend(note);
        }
    }
}

// ============================================================
// 12. 设备配置下发
// ============================================================

let configDeployState = {
    selectedDeviceIp: null,
    selectedConfigType: null,
    templates: {},
    clonePreview: null,
};

function updateConfigDeviceSelect() {
    const select = document.getElementById("config-device-select");
    if (!select) return;
    select.innerHTML = '<option value="">-- 请先选择设备 --</option>';

    // 检查是否有能力识别结果
    const hasCapabilityResults = Object.keys(capabilityResults).length > 0;
    const hintEl = document.getElementById("capability-hint");
    if (hintEl) {
        // 如果没有能力识别结果，显示提示
        if (!hasCapabilityResults && currentDevices.length > 0) {
            hintEl.classList.remove("hidden");
        } else {
            hintEl.classList.add("hidden");
        }
    }

    for (const device of currentDevices) {
        const cap = capabilityResults[device.ip];
        const role = cap ? (ROLE_NAMES[cap.device_role] || "") : "";
        const label = role ? `${device.name} (${device.ip}) - ${role}` : `${device.name} (${device.ip})`;
        select.innerHTML += `<option value="${escapeHtml(device.ip)}">${escapeHtml(label)}</option>`;
    }
    updateCloneTargetDevices();
}

async function onConfigDeviceSelected() {
    const select = document.getElementById("config-device-select");
    const ip = select.value;
    configDeployState.selectedDeviceIp = ip;
    configDeployState.selectedConfigType = null;
    configDeployState.clonePreview = null;
    document.getElementById("btn-deploy-clone").disabled = true;
    document.getElementById("clone-preview-area").classList.add("hidden");
    document.getElementById("clone-result-area").classList.add("hidden");
    updateCloneTargetDevices();

    document.getElementById("config-templates-area").classList.add("hidden");
    document.getElementById("config-form-area").classList.add("hidden");
    document.getElementById("config-preview-area").classList.add("hidden");
    document.getElementById("config-result-area").classList.add("hidden");

    if (!ip) return;

    // Load templates for this device's role
    const cap = capabilityResults[ip];
    const role = cap ? cap.device_role : "unknown";
    try {
        const data = await api(`/api/devices/capability-templates?device_role=${encodeURIComponent(role)}`);
        configDeployState.templates = data.templates || {};
        renderConfigTemplates(data.templates, cap);
        document.getElementById("config-templates-area").classList.remove("hidden");
    } catch (e) {
        console.error("加载配置模板失败:", e.message);
    }
}

function updateCloneTargetDevices() {
    const container = document.getElementById("clone-target-devices");
    const sourceLabel = document.getElementById("clone-source-device");
    if (!container || !sourceLabel) return;

    const sourceIp = configDeployState.selectedDeviceIp ||
        document.getElementById("config-device-select")?.value || "";
    const source = currentDevices.find(device => device.ip === sourceIp);
    sourceLabel.textContent = source ? `${source.name} (${source.ip})` : "请先在上方选择源设备";

    const targets = currentDevices.filter(device => device.ip !== sourceIp);
    if (!sourceIp || !targets.length) {
        container.innerHTML = '<span class="empty-hint">没有可选目标设备</span>';
        return;
    }

    container.innerHTML = targets.map(device => {
        const cap = capabilityResults[device.ip];
        const role = cap ? (ROLE_NAMES[cap.device_role] || cap.device_role || "") : "";
        return `<label>
            <input type="checkbox" value="${escapeHtml(device.ip)}">
            <span>${escapeHtml(device.name)} (${escapeHtml(device.ip)})${role ? ` · ${escapeHtml(role)}` : ""}</span>
        </label>`;
    }).join("");
}

function _getCloneSelection() {
    const sourceDevice = configDeployState.selectedDeviceIp ||
        document.getElementById("config-device-select").value;
    const targetDevices = Array.from(
        document.querySelectorAll("#clone-target-devices input[type='checkbox']:checked")
    ).map(input => input.value);
    const domains = Array.from(
        document.querySelectorAll("#clone-domain-select input[type='checkbox']:checked")
    ).map(input => input.value);
    return {
        source_device: sourceDevice,
        target_devices: targetDevices,
        domains,
        auto_resolve: document.getElementById("clone-auto-resolve").checked,
        include_sensitive: false,
        refresh_config: true,
    };
}

async function previewCloneConfig() {
    const request = _getCloneSelection();
    if (!request.source_device) {
        alert("请先选择源设备");
        return;
    }
    if (!request.target_devices.length) {
        alert("请至少选择一台目标设备");
        return;
    }
    if (!request.domains.length) {
        alert("请至少选择一个复制范围");
        return;
    }

    setButtonLoading("btn-preview-clone", true);
    document.getElementById("btn-deploy-clone").disabled = true;
    try {
        const result = await api("/api/config/clone/preview", {
            method: "POST",
            body: JSON.stringify(request),
        });
        configDeployState.clonePreview = result;
        renderClonePreview(result);
        const blocking = !result.success || (result.conflicts?.errors || []).length > 0;
        document.getElementById("btn-deploy-clone").disabled = blocking;
    } catch (e) {
        configDeployState.clonePreview = null;
        alert("生成复制方案失败: " + e.message);
    } finally {
        setButtonLoading("btn-preview-clone", false);
    }
}

function renderClonePreview(result) {
    const content = document.getElementById("clone-preview-content");
    let html = "";

    const warnings = result.warnings || [];
    const conflicts = result.conflicts || {};
    const conflictErrors = conflicts.errors || [];
    const conflictWarnings = conflicts.warnings || [];
    if (conflictErrors.length) {
        html += conflictErrors.map(item =>
            `<div class="conflict-item conflict-error"><span class="conflict-icon">×</span><span>${escapeHtml(item.message || String(item))}</span></div>`
        ).join("");
    }
    if (warnings.length || conflictWarnings.length) {
        html += [...warnings, ...conflictWarnings.map(item => item.message || String(item))].map(message =>
            `<div class="conflict-item conflict-warning"><span class="conflict-icon">!</span><span>${escapeHtml(message)}</span></div>`
        ).join("");
    }

    for (const [ip, target] of Object.entries(result.targets || {})) {
        const commands = target.commands || [];
        const resolutions = target.resolutions || [];
        const skipped = target.skipped || [];
        html += `<div class="clone-target-preview">
            <div class="clone-target-header">
                <strong>${escapeHtml(target.device_name || ip)} (${escapeHtml(ip)})</strong>
                <span>${commands.length} 条命令 · 自动处理 ${resolutions.length} 项 · 跳过 ${skipped.length} 项</span>
            </div>
            <div class="clone-resolution-list">
                ${resolutions.slice(0, 12).map(item => `<div class="clone-resolution">✓ ${escapeHtml(item.message || "")}</div>`).join("")}
                ${skipped.filter(item => item.type !== "domain_not_selected").slice(0, 8).map(item => `<div class="clone-skipped">– ${escapeHtml(item.message || "")}</div>`).join("")}
            </div>
            <pre class="config-block-pre">${escapeHtml(commands.join("\n") || "无需下发配置")}</pre>
        </div>`;
    }

    content.innerHTML = html;
    document.getElementById("clone-preview-area").classList.remove("hidden");
    document.getElementById("clone-result-area").classList.add("hidden");
}

async function deployCloneConfig() {
    const preview = configDeployState.clonePreview;
    if (!preview?.preview_id) {
        alert("请先生成复制方案");
        return;
    }

    const targetCount = Object.keys(preview.targets || {}).length;
    const confirmed = await showConfirm(
        "确认复制设备配置",
        `即将把源设备配置复制到 ${targetCount} 台目标设备。\n系统会再次备份目标设备，并仅下发预览中的命令。\n\n是否确认继续？`
    );
    if (!confirmed) return;

    setButtonLoading("btn-deploy-clone", true);
    try {
        const result = await api("/api/config/clone/deploy", {
            method: "POST",
            body: JSON.stringify({
                preview_id: preview.preview_id,
                confirm: true,
                save_config: document.getElementById("clone-save-config").checked,
            }),
        });
        renderCloneDeployResult(result);
    } catch (e) {
        renderCloneDeployResult({ success: false, error: e.message, results: {}, summary: {} });
    } finally {
        setButtonLoading("btn-deploy-clone", false);
    }
}

function renderCloneDeployResult(result) {
    const content = document.getElementById("clone-result-content");
    const summary = result.summary || {};
    let html = `<div class="${result.success ? "result-success" : "result-error"}">
        ${result.success ? "配置复制下发成功" : "配置复制下发未全部成功"}
        ${summary.total !== undefined ? ` · 成功 ${summary.success || 0}/${summary.total}` : ""}
    </div>`;
    if (result.error) {
        html += `<div class="result-row"><div class="result-label">错误</div><div class="result-value">${escapeHtml(result.error)}</div></div>`;
    }
    for (const [ip, deviceResult] of Object.entries(result.results || {})) {
        html += `<div class="result-row">
            <div class="result-label">${escapeHtml(deviceResult.device_name || ip)} (${escapeHtml(ip)})</div>
            <div class="result-value">${escapeHtml(deviceResult.success ? "成功" : (deviceResult.errors || []).join("\n"))}</div>
        </div>`;
    }
    content.innerHTML = html;
    document.getElementById("clone-result-area").classList.remove("hidden");
}

function renderConfigTemplates(templates, capabilities) {
    const grid = document.getElementById("config-templates-grid");
    const caps = capabilities ? capabilities.capabilities : {};

    // Group templates by category
    const categories = {};
    const categoryNames = {
        solution: "组合业务方案",
        routing: "路由协议",
        switching: "交换功能",
        network: "基础网络",
        security: "安全管理",
        vpn: "VPN/隧道",
    };

    Object.entries(templates).forEach(([key, tpl]) => {
        const cat = tpl.category || "other";
        if (!categories[cat]) categories[cat] = [];
        categories[cat].push({ key, tpl });
    });

    // Render grouped by category
    let html = "";
    const categoryOrder = ["solution", "routing", "switching", "network", "security", "vpn", "other"];
    categoryOrder.forEach(cat => {
        if (!categories[cat]) return;
        const catName = categoryNames[cat] || "其他";
        html += `<div class="config-category-group">
            <div class="config-category-title">${catName}</div>
            <div class="config-category-cards">`;
        categories[cat].forEach(({ key, tpl }) => {
            const cap = caps[key];
            const isSupported = !cap || cap.supported;
            const disabled = !isSupported;
            html += `<div class="config-template-card ${disabled ? 'disabled' : ''}"
                        data-type="${key}"
                        onclick="${disabled ? '' : `selectConfigTemplate('${key}')`}"
                        ${disabled ? 'title="设备不支持此功能"' : ''}>
                <span class="tpl-icon">${tpl.icon || ""}</span>
                <span class="tpl-name">${escapeHtml(tpl.name)}</span>
                ${disabled ? '<span class="cap-badge unsupported" style="display:block;margin-top:4px">不支持</span>' : ''}
            </div>`;
        });
        html += `</div></div>`;
    });

    grid.innerHTML = html;
}

function selectConfigTemplate(configType) {
    configDeployState.selectedConfigType = configType;

    // Highlight selected
    document.querySelectorAll(".config-template-card").forEach(card => {
        card.classList.toggle("active", card.dataset.type === configType);
    });

    // Render form
    const tpl = configDeployState.templates[configType];
    if (!tpl) return;

    // 打开弹窗
    const modal = document.getElementById("config-deploy-modal");
    modal.classList.remove("hidden");

    const container = document.getElementById("config-modal-form-fields");
    document.getElementById("config-modal-title").textContent = tpl.name;

    // 隐藏预览和结果区域
    document.getElementById("config-modal-preview-area").classList.add("hidden");
    document.getElementById("config-modal-result-area").classList.add("hidden");

    // 加载当前设备配置
    loadCurrentDeviceConfig();

    // 获取当前设备的能力识别结果（包含接口列表）
    const ip = configDeployState.selectedDeviceIp;
    const cap = ip ? capabilityResults[ip] : null;
    const interfaces = cap ? (cap.interfaces || []) : [];

    // 检查是否有字段分组
    if (tpl.field_groups && tpl.field_groups.length > 0) {
        // 分组渲染
        let html = '';
        tpl.field_groups.forEach(group => {
            const groupFields = tpl.fields.filter(f => f.group === group.id);
            if (groupFields.length === 0) return;

            // 基础配置默认展开，其他折叠
            const isBasic = group.id === 'basic' || group.id === 'network';
            html += `<div class="field-group" data-group="${group.id}">
                <div class="field-group-header" onclick="toggleFieldGroup(this)">
                    <span class="field-group-arrow">${isBasic ? '&#9660;' : '&#9654;'}</span>
                    <span class="field-group-icon">${group.icon || ''}</span>
                    <span class="field-group-name">${escapeHtml(group.name)}</span>
                </div>
                <div class="field-group-body ${isBasic ? 'open' : 'collapsed'}">
                    <div class="form-grid">`;

            groupFields.forEach(field => {
                html += renderFormField(field, interfaces);
            });

            html += `</div></div></div>`;
        });
        container.innerHTML = html;
    } else {
        // 兼容旧格式：扁平渲染
        container.innerHTML = tpl.fields.map(field => {
            return renderFormField(field, interfaces);
        }).join("");
    }

    // 绑定表单字段自动填充（如 network → wildcard）
    _bindFormAutoFill();
}

async function loadCurrentDeviceConfig() {
    const ip = configDeployState.selectedDeviceIp;
    const statusEl = document.getElementById("config-modal-current-status");
    const contentEl = document.getElementById("config-modal-current-content");

    if (!ip) {
        statusEl.textContent = "未选择设备";
        contentEl.textContent = "请先选择设备";
        return;
    }

    statusEl.textContent = "加载中...";
    contentEl.textContent = "正在获取设备配置...";

    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/current-config`);
        if (data.success && data.config) {
            const config = data.config;
            const displayConfig = config.length > 2000
                ? config.substring(0, 2000) + "\n\n... (配置过长，已截断)"
                : config;
            contentEl.textContent = displayConfig;
            statusEl.title = (data.warnings || []).join("\n");
            if (data.source === "backup") {
                statusEl.textContent = "历史备份";
                statusEl.className = "badge badge-warning";
            } else if (data.source === "live") {
                statusEl.textContent = "实时已加载";
                statusEl.className = "badge badge-success";
            } else {
                statusEl.textContent = "缓存已加载";
                statusEl.className = "badge badge-success";
            }
            return;
        }

        contentEl.textContent = "无法获取设备配置";
        statusEl.textContent = "获取失败";
        statusEl.className = "badge badge-danger";
    } catch (e) {
        contentEl.textContent = "获取配置失败: " + e.message;
        statusEl.textContent = "错误";
        statusEl.className = "badge badge-danger";
    }
}

function closeConfigDeployModal() {
    document.getElementById("config-deploy-modal").classList.add("hidden");
}

function getModalFormParams() {
    const container = document.getElementById("config-modal-form-fields");
    const inputs = container.querySelectorAll(".protocol-input, .protocol-checkbox");
    const params = {};
    inputs.forEach(input => {
        if (input.type === "checkbox") {
            params[input.name] = input.checked;
        } else {
            params[input.name] = input.value;
        }
    });
    return params;
}

async function previewConfigModal() {
    const ip = configDeployState.selectedDeviceIp;
    const configType = configDeployState.selectedConfigType;
    if (!ip || !configType) return;

    const params = getModalFormParams();
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/config/preview`, {
            method: "POST",
            body: JSON.stringify({ config_type: configType, params }),
        });
        const previewEl = document.getElementById("config-modal-preview-content");
        previewEl.textContent = (data.preview_commands || []).join("\n");
        document.getElementById("config-modal-preview-area").classList.remove("hidden");
    } catch (e) {
        alert("预览失败: " + e.message);
    }
}

async function deployConfigModal() {
    const ip = configDeployState.selectedDeviceIp;
    const configType = configDeployState.selectedConfigType;
    if (!ip || !configType) return;

    const params = getModalFormParams();
    const saveConfig = document.getElementById("config-modal-save").checked;

    setButtonLoading("btn-deploy-config-modal", true);
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/config/deploy`, {
            method: "POST",
            body: JSON.stringify({ config_type: configType, params, save_config: saveConfig }),
        });
        renderDeployResult("config-modal-result-area", "config-modal-result-content", data);
    } catch (e) {
        renderDeployResult("config-modal-result-area", "config-modal-result-content", {
            success: false, error: e.message
        });
    } finally {
        setButtonLoading("btn-deploy-config-modal", false);
    }
}

function renderFormField(field, interfaces) {
    const requiredMark = field.required ? '<span class="required">*</span>' : '';
    let input = '';

    // 如果是接口字段且有接口列表，显示为下拉选择框
    if (['interface', 'access_interface', 'outside_interface'].includes(field.name) && interfaces && interfaces.length > 0) {
        input = `<select name="${field.name}" class="protocol-input">
            <option value="">-- 选择接口 --</option>
            ${interfaces.map(intf => {
                const intfName = intf.name;
                const ipInfo = intf.ip ? ` (${intf.ip})` : '';
                const statusInfo = intf.status ? ` [${intf.status}]` : '';
                return `<option value="${escapeHtml(intfName)}">${escapeHtml(intfName)}${ipInfo}${statusInfo}</option>`;
            }).join('')}
        </select>`;
    } else if (field.type === 'select') {
        input = `<select name="${field.name}" class="protocol-input">
            ${field.options.map(opt => `<option value="${opt.value}" ${opt.value === (field.default || '') ? 'selected' : ''}>${escapeHtml(opt.label)}</option>`).join('')}
        </select>`;
    } else if (field.type === 'checkbox') {
        input = `<input type="checkbox" name="${field.name}" ${field.default ? 'checked' : ''} class="protocol-checkbox" style="width:18px;height:18px">`;
    } else {
        // 构建属性
        let attrs = `type="${field.type}" name="${field.name}"
            value="${escapeHtml(String(field.default || ''))}"
            placeholder="${escapeHtml(field.placeholder || '')}"
            ${field.required ? 'required' : ''}
            class="protocol-input"`;

        // 如果有自动填充标记，添加 data 属性（在渲染后绑定事件）
        if (field.auto_fill_target) {
            attrs += ` data-auto-fill-target="${escapeHtml(field.auto_fill_target)}"`;
            attrs += ` data-auto-fill-type="${escapeHtml(field.auto_fill_type || 'network_to_wildcard')}"`;
        }

        input = `<input ${attrs}>`;
    }

    return `<div class="form-row">
        <label>${escapeHtml(field.label)}${requiredMark}</label>
        ${input}
    </div>`;
}

// 模板表单字段自动填充事件绑定
// 在 selectConfigTemplate 渲染完表单后调用
function _bindFormAutoFill() {
    const container = document.getElementById("config-modal-form-fields");
    if (!container) return;

    container.querySelectorAll("input[data-auto-fill-target]").forEach(input => {
        const targetName = input.dataset.autoFillTarget;
        const fillType = input.dataset.autoFillType;

        input.addEventListener("input", () => {
            const targetInput = container.querySelector(`input[name="${targetName}"], select[name="${targetName}"]`);
            if (!targetInput) return;

            // 只在目标字段为空或看起来是自动生成的值时才覆盖
            const currentVal = targetInput.value.trim();
            const shouldFill = !currentVal || currentVal === "0.0.0.255" || currentVal === "0.0.255.255" || currentVal === "0.255.255.255";

            if (fillType === "network_to_wildcard" && shouldFill) {
                const wildcard = networkToWildcardJS(input.value);
                if (wildcard) {
                    targetInput.value = wildcard;
                }
            }
        });
    });
}

function toggleFieldGroup(header) {
    const body = header.nextElementSibling;
    const arrow = header.querySelector('.field-group-arrow');
    body.classList.toggle('collapsed');
    body.classList.toggle('open');
    arrow.innerHTML = body.classList.contains('open') ? '&#9660;' : '&#9654;';
}

function _getFormParams() {
    const container = document.getElementById("config-form-fields");
    const inputs = container.querySelectorAll(".protocol-input, .protocol-checkbox");
    const params = {};
    inputs.forEach(input => {
        if (input.type === "checkbox") {
            params[input.name] = input.checked;
        } else {
            params[input.name] = input.value;
        }
    });
    return params;
}

async function previewConfig() {
    const ip = configDeployState.selectedDeviceIp;
    const configType = configDeployState.selectedConfigType;
    if (!ip || !configType) return;

    const params = _getFormParams();
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/config/preview`, {
            method: "POST",
            body: JSON.stringify({ config_type: configType, params }),
        });
        const previewEl = document.getElementById("config-preview-content");
        previewEl.textContent = (data.preview_commands || []).join("\n");
        document.getElementById("config-preview-area").classList.remove("hidden");
    } catch (e) {
        alert("预览失败: " + e.message);
    }
}

async function deployConfig() {
    const ip = configDeployState.selectedDeviceIp;
    const configType = configDeployState.selectedConfigType;
    if (!ip || !configType) return;

    const confirmed = await showConfirm(
        "确认下发配置",
        "即将修改设备配置，系统会先备份当前配置。是否确认继续？"
    );
    if (!confirmed) return;

    const params = _getFormParams();
    const saveConfig = false; // template mode doesn't auto-save unless checkbox

    setButtonLoading("btn-deploy-config", true);
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/config/deploy`, {
            method: "POST",
            body: JSON.stringify({ config_type: configType, params, save_config: saveConfig }),
        });
        renderDeployResult("config-result-area", "config-result-content", data);
    } catch (e) {
        renderDeployResult("config-result-area", "config-result-content", {
            success: false, error: e.message
        });
    } finally {
        setButtonLoading("btn-deploy-config", false);
    }
}

async function previewConsoleConfig() {
    const port = document.getElementById("console-port-input").value.trim();
    const raw = document.getElementById("console-commands").value;
    const commands = raw.split("\n").map(s => s.trim()).filter(Boolean);
    if (!port) { alert("请填写 Console 端口"); return; }
    if (!commands.length) { alert("请填写配置命令"); return; }
    const previewEl = document.getElementById("console-preview-content");
    previewEl.textContent = commands.join("\n");
    document.getElementById("console-preview-area").classList.remove("hidden");
}

async function deployConsoleConfig() {
    const port = document.getElementById("console-port-input").value.trim();
    const raw = document.getElementById("console-commands").value;
    const commands = raw.split("\n").map(s => s.trim()).filter(Boolean);
    const saveConfig = document.getElementById("console-save-config").checked;
    if (!port) { alert("请填写 Console 端口"); return; }
    if (!commands.length) { alert("请填写配置命令"); return; }

    const confirmed = await showConfirm(
        "确认通过 Console 下发配置",
        "即将通过 Console 端口下发命令，该通道不会自动备份配置（无 SSH 会话）。是否确认继续？"
    );
    if (!confirmed) return;

    setButtonLoading("btn-deploy-console", true);
    try {
        const data = await api("/api/console-config/raw-deploy", {
            method: "POST",
            body: JSON.stringify({ console_port: parseInt(port, 10), commands, save_config: saveConfig }),
        });
        renderDeployResult("console-result-area", "console-result-content", data);
    } catch (e) {
        renderDeployResult("console-result-area", "console-result-content", {
            success: false, error: e.message
        });
    } finally {
        setButtonLoading("btn-deploy-console", false);
    }
}

function renderDeployResult(areaId, contentId, data) {
    const area = document.getElementById(areaId);
    const content = document.getElementById(contentId);

    // 记录最近一次下发结果，供 AI 助教"诊断报错"使用
    window._aiLastDeploy = {
        ip: configDeployState.selectedDeviceIp || window._aiLastDeploy?.ip || "",
        success: !!data.success,
        error: data.success ? "" : (data.error || "下发失败（无详细错误信息）"),
        outputs: (data.command_outputs || []).map(o =>
            `${o.success ? '✓' : '✗'} ${o.command}\n${o.output || o.error || ''}`).join('\n\n').slice(0, 2000),
    };

    let html = '';

    if (data.backup_file) {
        html += `<div class="result-row"><div class="result-label">备份文件</div>
            <div class="result-value">${escapeHtml(data.backup_file)}</div></div>`;
    }

    if (data.commands && data.commands.length) {
        html += `<div class="result-row"><div class="result-label">下发命令</div>
            <div class="result-value">${escapeHtml(data.commands.join('\n'))}</div></div>`;
    }

    if (data.command_outputs && data.command_outputs.length) {
        const outputs = data.command_outputs.map(o => {
            const status = o.success ? '✓' : '✗';
            return `${status} ${o.command}\n${o.output || o.error || ''}`;
        }).join('\n\n');
        html += `<div class="result-row"><div class="result-label">执行回显</div>
            <div class="result-value">${escapeHtml(outputs)}</div></div>`;
    }

    if (data.verify_outputs && data.verify_outputs.length) {
        const vOutputs = data.verify_outputs.map(o => {
            const status = o.success ? '✓' : '✗';
            return `${status} ${o.command}\n${o.output || o.error || ''}`;
        }).join('\n\n');
        html += `<div class="result-row"><div class="result-label">验证结果</div>
            <div class="result-value">${escapeHtml(vOutputs)}</div></div>`;
    }

    if (data.save_result) {
        html += `<div class="result-row"><div class="result-label">保存配置</div>
            <div class="result-value">${escapeHtml(data.save_result)}</div></div>`;
    }

    const successClass = data.success ? 'result-success' : 'result-error';
    const statusText = data.success ? '下发成功' : `下发失败: ${escapeHtml(data.error || '未知错误')}`;
    html += `<div class="result-row"><div class="result-label">状态</div>
        <div class="${successClass}" style="font-size:14px;font-weight:700;padding:8px 0">${statusText}</div></div>`;
    if (!data.success) {
        html += `<button class="btn btn-sm btn-primary" onclick="openAIDiagnose()">
            <span>&#129302;</span> AI 诊断这次失败</button>`;
    }

    content.innerHTML = html;
    area.classList.remove("hidden");
}

// ============================================================
// 13. 高级命令模式
// ============================================================

async function previewRawCommands() {
    const ip = configDeployState.selectedDeviceIp;
    if (!ip) { alert("请先选择设备"); return; }

    const commandsText = document.getElementById("raw-commands").value.trim();
    if (!commandsText) { alert("请输入命令"); return; }

    const commands = commandsText.split("\n").filter(c => c.trim());
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/config/raw-preview`, {
            method: "POST",
            body: JSON.stringify({ commands }),
        });
        document.getElementById("raw-preview-content").textContent =
            (data.preview_commands || data.commands || []).join("\n") +
            "\n\n--- " + (data.warning || "") + " ---";
        document.getElementById("raw-preview-area").classList.remove("hidden");
    } catch (e) {
        alert("预览失败: " + e.message);
    }
}

async function deployRawCommands() {
    const ip = configDeployState.selectedDeviceIp;
    if (!ip) { alert("请先选择设备"); return; }

    const commandsText = document.getElementById("raw-commands").value.trim();
    if (!commandsText) { alert("请输入命令"); return; }

    const commands = commandsText.split("\n").filter(c => c.trim());

    const confirmed1 = await showConfirm(
        "确认下发原始命令",
        "高级命令模式将直接下发原始命令到设备，可能导致配置异常。系统会先自动备份当前配置。是否继续？"
    );
    if (!confirmed1) return;

    const confirmed2 = await showConfirm(
        "二次确认",
        "这是最后确认。确认下发 " + commands.length + " 条命令到设备？"
    );
    if (!confirmed2) return;

    const saveConfig = document.getElementById("raw-save-config").checked;

    setButtonLoading("btn-deploy-raw", true);
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/config/raw-deploy`, {
            method: "POST",
            body: JSON.stringify({ commands, save_config: saveConfig, confirm: true }),
        });
        renderDeployResult("raw-result-area", "raw-result-content", data);
        if (data.warning) {
            document.getElementById("raw-result-content").innerHTML +=
                `<div class="info-bar warning" style="margin-top:8px">${escapeHtml(data.warning)}</div>`;
        }
    } catch (e) {
        renderDeployResult("raw-result-area", "raw-result-content", {
            success: false, error: e.message
        });
    } finally {
        setButtonLoading("btn-deploy-raw", false);
    }
}

// ============================================================
// 14. 备份管理
// ============================================================

async function loadBackupList() {
    const ip = configDeployState.selectedDeviceIp ||
        document.getElementById("config-device-select").value;
    if (!ip) { alert("请先选择设备"); return; }

    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/backups`);
        const backups = data.backups || [];
        const content = document.getElementById("backup-list-content");

        if (!backups.length) {
            content.innerHTML = '<p class="empty-state">暂无备份文件</p>';
        } else {
            content.innerHTML = backups.map(b =>
                `<div class="backup-item">
                    <span class="backup-name">${escapeHtml(b.filename)}</span>
                    <span class="backup-meta">
                        <span>${b.size} bytes</span>
                        <span>${escapeHtml(b.created)}</span>
                    </span>
                </div>`
            ).join("");
        }
        document.getElementById("backup-list-area").classList.remove("hidden");
    } catch (e) {
        console.error("加载备份列表失败:", e.message);
    }
}

// ============================================================
// 15. 页面初始化
// ============================================================

function initApp() {
    initSidebar();
    checkServerInitStatus();
    updateWorkbenchDeviceSelect();
}

async function checkServerInitStatus() {
    try {
        const data = await api("/api/init-status");
        if (data.initialized) {
            // 服务器已完成初始化，直接加载数据
            loadServerData();
        } else if (data.in_progress) {
            // 初始化进行中，显示进度
            showStatus("batch-status", data.message || "正在初始化...", "loading");
            pollInitStatus();
        } else {
            // 未初始化，触发初始化
            showStatus("batch-status", "首次启动，正在自动初始化...", "loading");
            await api("/api/trigger-init", { method: "POST" });
            pollInitStatus();
        }
    } catch (e) {
        console.error("检查初始化状态失败:", e);
        // 尝试直接加载数据
        loadServerData();
    }
}

function pollInitStatus() {
    const interval = setInterval(async () => {
        try {
            const data = await api("/api/init-status");
            showStatus("batch-status", data.message || "正在初始化...", "loading");

            if (data.initialized) {
                clearInterval(interval);
                showStatus("batch-status", "初始化完成，正在加载数据...", "success");
                setTimeout(() => loadServerData(), 500);
            }
        } catch (e) {
            console.error("轮询初始化状态失败:", e);
        }
    }, 2000);
}

async function loadServerData() {
    try {
        const data = await api("/api/server-data");
        if (data.success) {
            // 恢复网卡选择
            if (data.selected_interface) {
                const info = document.getElementById("selected-info");
                info.hidden = false;
                document.getElementById("selected-name").textContent = data.selected_interface.name;
                document.getElementById("selected-ip").textContent = data.selected_interface.ip;
                document.getElementById("selected-subnet").textContent = data.subnet || "";
                selectedSubnet = data.subnet || "";
                info.className = "info-bar success";
            }

            // 恢复设备列表
            currentDevices = data.devices || [];
            renderDevices(currentDevices);

            // 恢复采集结果
            collectionResults = data.collection_results || {};
            renderDeviceCards(collectionResults);

            // 恢复能力识别结果
            capabilityResults = data.capability_results || {};
            if (Object.keys(capabilityResults).length) {
                renderCapabilityResults(Object.values(capabilityResults));
            }

            // 更新各处的设备选择
            updateConfigDeviceSelect();
            updateWorkbenchDeviceSelect();

            // 加载网卡列表（服务器启动时已自动扫描，这里恢复 UI 状态）
            loadInterfaces();

            // 加载拓扑
            loadTopology({ silent: true });

            hideStatus("batch-status");
            console.log("服务器数据加载完成");
        }
    } catch (e) {
        console.error("加载服务器数据失败:", e);
        showStatus("batch-status", "加载数据失败: " + e.message, "error");
    }
}

// ============================================================
// 16. 自由配置工作台
// ============================================================

let workbenchState = {
    lastPreview: null,
    varMode: "table",      // "table" 或 "json"
    varTableData: {},       // {varName: {global: "", devices: {ip: ""}}}
    smartDefaults: {},      // 智能默认值缓存 {ip: {router_id, ...}}
};

function updateWorkbenchDeviceSelect() {
    const container = document.getElementById("wb-device-select");
    if (!container) return;

    if (!currentDevices || !currentDevices.length) {
        container.innerHTML = '<p class="empty-hint">暂无设备，请先扫描或添加设备</p>';
        return;
    }

    container.innerHTML = currentDevices.map(d => {
        const name = d.name || d.ip;
        return `<label class="wb-device-cb">
            <input type="checkbox" value="${escapeHtml(d.ip)}" onchange="workbenchDeviceChanged()">
            <span class="wb-device-name">${escapeHtml(name)}</span>
            <span class="wb-device-ip">${escapeHtml(d.ip)}</span>
        </label>`;
    }).join("");
}

// 监听命令模板变化，自动提取变量
document.addEventListener("DOMContentLoaded", () => {
    const cmdEditor = document.getElementById("wb-commands");
    if (cmdEditor) {
        let debounceTimer = null;
        cmdEditor.addEventListener("input", () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                if (workbenchState.varMode === "table") {
                    buildVariableTable();
                    loadSmartDefaults();
                }
            }, 600);
        });
    }
});

function workbenchDeviceChanged() {
    // Clear preview/result when device selection changes
    document.getElementById("wb-preview-area").classList.add("hidden");
    document.getElementById("wb-result-area").classList.add("hidden");
    workbenchState.lastPreview = null;

    // 重建变量表格的设备列
    if (workbenchState.varMode === "table") {
        rebuildVarTableColumns();
    }
}

// ============================================================
// 变量表格功能
// ============================================================

function _getSelectedDeviceIps() {
    const checkboxes = document.querySelectorAll("#wb-device-select input[type='checkbox']:checked");
    return Array.from(checkboxes).map(cb => cb.value);
}

function _getSelectedDeviceInfo(ip) {
    return currentDevices.find(d => d.ip === ip) || { name: ip, ip };
}

function rebuildVarTableColumns() {
    const devices = _getSelectedDeviceIps();
    const headRow = document.getElementById("wb-var-table-head");
    const body = document.getElementById("wb-var-table-body");

    // 清空旧的设备列头（保留变量名、全局默认值、操作）
    while (headRow.children.length > 3) {
        headRow.removeChild(headRow.children[2]);
    }

    // 插入新的设备列头
    const insertBefore = headRow.children[2]; // "操作" 列
    devices.forEach(ip => {
        const info = _getSelectedDeviceInfo(ip);
        const th = document.createElement("th");
        th.textContent = `${info.name || ip}`;
        th.title = ip;
        th.dataset.deviceIp = ip;
        headRow.insertBefore(th, insertBefore);
    });

    // 为现有行添加/更新设备列
    const rows = body.querySelectorAll("tr:not(.wb-var-empty-row)");
    rows.forEach(row => _syncRowDeviceCells(row, devices));
}

function _syncRowDeviceCells(row, devices) {
    // 删除旧的设备列（从后往前删，保留前2列和最后1列操作列）
    const cells = row.children;
    while (cells.length > 3) {
        row.removeChild(cells[2]);
    }

    // 在操作列前插入新的设备单元格
    const actionCell = row.lastElementChild;
    const varName = row.querySelector("input[data-role='var-name']");
    const savedDevices = varName ? (workbenchState.varTableData[varName.value]?.devices || {}) : {};

    devices.forEach(ip => {
        const td = document.createElement("td");
        const input = document.createElement("input");
        input.type = "text";
        input.className = "protocol-input wb-var-cell";
        input.dataset.deviceIp = ip;
        input.dataset.role = "device-value";
        input.placeholder = ip;
        input.value = savedDevices[ip] || "";
        input.addEventListener("input", () => _onVarCellInput(input));
        td.appendChild(input);
        row.insertBefore(td, actionCell);
    });
}

function _extractVariablesFromTemplate() {
    const template = document.getElementById("wb-commands").value;
    const pattern = /\{\{(\w+)\}\}/g;
    const vars = new Set();
    let match;
    while ((match = pattern.exec(template)) !== null) {
        vars.add(match[1]);
    }
    return Array.from(vars);
}

function buildVariableTable() {
    const body = document.getElementById("wb-var-table-body");
    const devices = _getSelectedDeviceIps();
    const vars = _extractVariablesFromTemplate();

    // 保留已有数据
    const oldData = {};
    body.querySelectorAll("tr:not(.wb-var-empty-row)").forEach(row => {
        const nameInput = row.querySelector("input[data-role='var-name']");
        const globalInput = row.querySelector("input[data-role='global-value']");
        if (nameInput && nameInput.value) {
            oldData[nameInput.value] = {
                global: globalInput?.value || "",
                devices: {},
            };
            row.querySelectorAll("input[data-role='device-value']").forEach(inp => {
                if (inp.dataset.deviceIp) {
                    oldData[nameInput.value].devices[inp.dataset.deviceIp] = inp.value;
                }
            });
        }
    });

    // 合并数据（保留旧值，新增变量用智能默认值）
    body.innerHTML = "";
    if (vars.length === 0) {
        body.innerHTML = '<tr class="wb-var-empty-row"><td colspan="3" style="text-align:center;color:var(--text-muted)">在命令模板中使用 {{变量名}} 后，点击「提取变量」自动填充</td></tr>';
        rebuildVarTableColumns();
        return;
    }

    vars.forEach(varName => {
        const existing = oldData[varName];
        const globalVal = existing?.global || workbenchState.varTableData[varName]?.global || "";

        // 更新内存缓存
        if (!workbenchState.varTableData[varName]) {
            workbenchState.varTableData[varName] = { global: globalVal, devices: {} };
        }
        if (existing?.devices) {
            Object.assign(workbenchState.varTableData[varName].devices, existing.devices);
        }

        const row = document.createElement("tr");

        // 变量名
        const tdName = document.createElement("td");
        tdName.className = "var-name-cell";
        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.className = "protocol-input";
        nameInput.dataset.role = "var-name";
        nameInput.value = varName;
        nameInput.addEventListener("change", () => {
            // 重命名时迁移数据
            const oldVal = workbenchState.varTableData[varName];
            if (oldVal) {
                workbenchState.varTableData[nameInput.value] = oldVal;
                delete workbenchState.varTableData[varName];
            }
        });
        tdName.appendChild(nameInput);
        row.appendChild(tdName);

        // 全局默认值
        const tdGlobal = document.createElement("td");
        const globalInput = document.createElement("input");
        globalInput.type = "text";
        globalInput.className = "protocol-input";
        globalInput.dataset.role = "global-value";
        globalInput.value = globalVal;
        globalInput.placeholder = "所有设备共用";
        globalInput.addEventListener("input", () => {
            if (workbenchState.varTableData[varName]) {
                workbenchState.varTableData[varName].global = globalInput.value;
            }
        });
        tdGlobal.appendChild(globalInput);
        row.appendChild(tdGlobal);

        // 操作列
        const tdAction = document.createElement("td");
        const delBtn = document.createElement("button");
        delBtn.className = "btn btn-sm btn-outline btn-danger-icon";
        delBtn.textContent = "×";
        delBtn.title = "删除变量";
        delBtn.onclick = () => {
            row.remove();
            delete workbenchState.varTableData[nameInput.value];
            const bodyRows = body.querySelectorAll("tr:not(.wb-var-empty-row)");
            if (bodyRows.length === 0) {
                body.innerHTML = '<tr class="wb-var-empty-row"><td colspan="3" style="text-align:center;color:var(--text-muted)">在命令模板中使用 {{变量名}} 后，点击「提取变量」自动填充</td></tr>';
            }
        };
        tdAction.appendChild(delBtn);
        row.appendChild(tdAction);

        body.appendChild(row);
    });

    // 添加设备列
    rebuildVarTableColumns();
}

function addVariableRow() {
    const body = document.getElementById("wb-var-table-body");
    const emptyRow = body.querySelector(".wb-var-empty-row");
    if (emptyRow) emptyRow.remove();

    const devices = _getSelectedDeviceIps();
    const row = document.createElement("tr");

    // 变量名
    const tdName = document.createElement("td");
    tdName.className = "var-name-cell";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "protocol-input";
    nameInput.dataset.role = "var-name";
    nameInput.placeholder = "变量名";
    tdName.appendChild(nameInput);
    row.appendChild(tdName);

    // 全局默认值
    const tdGlobal = document.createElement("td");
    const globalInput = document.createElement("input");
    globalInput.type = "text";
    globalInput.className = "protocol-input";
    globalInput.dataset.role = "global-value";
    globalInput.placeholder = "所有设备共用";
    tdGlobal.appendChild(globalInput);
    row.appendChild(tdGlobal);

    // 设备列
    devices.forEach(ip => {
        const td = document.createElement("td");
        const input = document.createElement("input");
        input.type = "text";
        input.className = "protocol-input wb-var-cell";
        input.dataset.deviceIp = ip;
        input.dataset.role = "device-value";
        input.placeholder = ip;
        input.addEventListener("input", () => _onVarCellInput(input));
        td.appendChild(input);
        row.appendChild(td);
    });

    // 操作列
    const tdAction = document.createElement("td");
    const delBtn = document.createElement("button");
    delBtn.className = "btn btn-sm btn-outline btn-danger-icon";
    delBtn.textContent = "×";
    delBtn.title = "删除变量";
    delBtn.onclick = () => {
        row.remove();
        const bodyRows = body.querySelectorAll("tr:not(.wb-var-empty-row)");
        if (bodyRows.length === 0) {
            body.innerHTML = '<tr class="wb-var-empty-row"><td colspan="3" style="text-align:center;color:var(--text-muted)">在命令模板中使用 {{变量名}} 后，点击「提取变量」自动填充</td></tr>';
        }
    };
    tdAction.appendChild(delBtn);
    row.appendChild(tdAction);

    body.appendChild(row);

    // 聚焦到变量名输入框
    nameInput.focus();
}

function syncVarTableToJson() {
    if (workbenchState.varMode !== "table") return;

    const body = document.getElementById("wb-var-table-body");
    const globalVars = {};
    const deviceVars = {};

    body.querySelectorAll("tr:not(.wb-var-empty-row)").forEach(row => {
        const nameInput = row.querySelector("input[data-role='var-name']");
        const globalInput = row.querySelector("input[data-role='global-value']");
        const varName = nameInput?.value?.trim();
        if (!varName) return;

        const globalVal = globalInput?.value?.trim() || "";
        if (globalVal) {
            globalVars[varName] = globalVal;
        }

        row.querySelectorAll("input[data-role='device-value']").forEach(inp => {
            const ip = inp.dataset.deviceIp;
            const val = inp.value.trim();
            if (!ip) return;
            if (!deviceVars[ip]) deviceVars[ip] = {};
            if (val) {
                deviceVars[ip][varName] = val;
            }
        });
    });

    document.getElementById("wb-global-vars").value = JSON.stringify(globalVars, null, 2);
    document.getElementById("wb-device-vars").value = JSON.stringify(deviceVars, null, 2);
}

function switchVarMode() {
    const btn = document.getElementById("btn-switch-var-mode");
    const tableContainer = document.getElementById("wb-var-table-container");
    const jsonContainer = document.getElementById("wb-json-vars-container");
    const modeLabel = document.getElementById("wb-var-mode-label");

    if (workbenchState.varMode === "table") {
        // 切换到 JSON 模式
        syncVarTableToJson();
        tableContainer.classList.add("hidden");
        jsonContainer.classList.remove("hidden");
        workbenchState.varMode = "json";
        btn.textContent = "切换到表格模式";
        modeLabel.textContent = "JSON 模式 — 手动编辑全局和单设备变量";
    } else {
        // 切换到表格模式：从 JSON 解析并填充表格
        _populateTableFromJson();
        jsonContainer.classList.add("hidden");
        tableContainer.classList.remove("hidden");
        workbenchState.varMode = "table";
        btn.textContent = "切换到 JSON 模式";
        modeLabel.textContent = "表格模式 — 点击变量名自动从模板提取";
    }
}

function _populateTableFromJson() {
    const globalStr = document.getElementById("wb-global-vars").value.trim();
    const deviceStr = document.getElementById("wb-device-vars").value.trim();
    let globalVars = {};
    let deviceVars = {};

    try { if (globalStr && globalStr !== "{}") globalVars = JSON.parse(globalStr); } catch (e) {}
    try { if (deviceStr && deviceStr !== "{}") deviceVars = JSON.parse(deviceStr); } catch (e) {}

    // 从 JSON 中提取变量名
    const allVarNames = new Set();
    Object.keys(globalVars).forEach(k => allVarNames.add(k));
    Object.values(deviceVars).forEach(dv => Object.keys(dv).forEach(k => allVarNames.add(k)));

    // 同时保留模板中的变量
    _extractVariablesFromTemplate().forEach(v => allVarNames.add(v));

    // 更新 varTableData
    workbenchState.varTableData = {};
    allVarNames.forEach(varName => {
        workbenchState.varTableData[varName] = {
            global: globalVars[varName] || "",
            devices: {},
        };
        for (const [ip, dv] of Object.entries(deviceVars)) {
            if (dv[varName]) {
                workbenchState.varTableData[varName].devices[ip] = dv[varName];
            }
        }
    });

    // 重建表格
    buildVariableTable();
}

// ============================================================
// 反掩码自动推导 + 智能默认值
// ============================================================

function networkToWildcardJS(network) {
    if (!network) return null;
    network = network.trim();

    // 已经是反掩码格式
    if (network.startsWith("0.")) {
        const parts = network.split(".");
        if (parts.length === 4 && parts.every(p => !isNaN(p) && +p >= 0 && +p <= 255)) {
            return network;
        }
    }

    // CIDR 格式
    if (network.includes("/")) {
        const [addr, prefixStr] = network.split("/");
        const prefix = parseInt(prefixStr, 10);
        if (isNaN(prefix) || prefix < 0 || prefix > 32) return null;
        const mask = (0xFFFFFFFF << (32 - prefix)) >>> 0;
        const wildcard = (~mask) >>> 0;
        return [
            (wildcard >>> 24) & 0xFF,
            (wildcard >>> 16) & 0xFF,
            (wildcard >>> 8) & 0xFF,
            wildcard & 0xFF,
        ].join(".");
    }

    // 纯 IP 格式，按类推断
    const octets = network.split(".");
    if (octets.length !== 4) return null;
    const first = parseInt(octets[0], 10);
    if (isNaN(first) || first === 0) return null;

    let prefix;
    if (first <= 127) prefix = 8;
    else if (first <= 191) prefix = 16;
    else prefix = 24;

    const mask = (0xFFFFFFFF << (32 - prefix)) >>> 0;
    const wildcard = (~mask) >>> 0;
    return [
        (wildcard >>> 24) & 0xFF,
        (wildcard >>> 16) & 0xFF,
        (wildcard >>> 8) & 0xFF,
        wildcard & 0xFF,
    ].join(".");
}

function _onVarCellInput(input) {
    const row = input.closest("tr");
    if (!row) return;
    const varNameInput = row.querySelector("input[data-role='var-name']");
    const varName = varNameInput?.value?.trim();
    if (!varName) return;

    // 如果输入的是 network / network_address 变量，自动推导 wildcard
    const networkVarNames = ["network", "network_address", "network_addr", "net_addr"];
    const wildcardVarNames = ["wildcard", "wildcard_mask", "wc"];

    if (networkVarNames.includes(varName)) {
        const val = input.value.trim();
        const wildcard = networkToWildcardJS(val);
        if (wildcard) {
            // 在同一行找到 wildcard 变量列并填充
            _findAndFillWildcardInSameRow(row, wildcardVarNames);
        }
    }

    // 同步到内存缓存
    const ip = input.dataset.deviceIp;
    if (ip && workbenchState.varTableData[varName]) {
        workbenchState.varTableData[varName].devices[ip] = input.value.trim();
    }
}

function _findAndFillWildcardInSameRow(row, wildcardVarNames) {
    // 遍历表格所有行找到 wildcard 变量
    const body = document.getElementById("wb-var-table-body");
    body.querySelectorAll("tr:not(.wb-var-empty-row)").forEach(r => {
        const nameInput = r.querySelector("input[data-role='var-name']");
        const vName = nameInput?.value?.trim();
        if (wildcardVarNames.includes(vName)) {
            // 检查全局值是否为空
            const globalInput = r.querySelector("input[data-role='global-value']");
            if (globalInput && !globalInput.value.trim()) {
                // 自动从当前行的 network 值推导
                const networkInput = row.querySelector("input[data-role='global-value']");
                if (networkInput && networkInput.value.trim()) {
                    const wc = networkToWildcardJS(networkInput.value.trim());
                    if (wc) globalInput.value = wc;
                }
            }
        }
    });
}

async function loadSmartDefaults() {
    const devices = _getSelectedDeviceIps();
    if (devices.length === 0) return;

    try {
        const data = await api("/api/config/smart-defaults", {
            method: "POST",
            body: JSON.stringify({ target_devices: devices }),
        });

        if (data.success && data.defaults) {
            workbenchState.smartDefaults = data.defaults;
            _applySmartDefaults();
        }
    } catch (e) {
        console.warn("加载智能默认值失败:", e.message);
    }
}

function _applySmartDefaults() {
    const body = document.getElementById("wb-var-table-body");
    const defaults = workbenchState.smartDefaults;

    body.querySelectorAll("tr:not(.wb-var-empty-row)").forEach(row => {
        const nameInput = row.querySelector("input[data-role='var-name']");
        const varName = nameInput?.value?.trim();
        if (!varName) return;

        // Router ID 自动填充
        if (varName === "router_id" || varName === "router-id") {
            row.querySelectorAll("input[data-role='device-value']").forEach(inp => {
                const ip = inp.dataset.deviceIp;
                if (ip && defaults[ip]?.router_id && !inp.value.trim()) {
                    inp.value = defaults[ip].router_id;
                }
            });
            const globalInput = row.querySelector("input[data-role='global-value']");
            if (globalInput && !globalInput.value.trim()) {
                // 使用第一台设备的值作为默认
                const firstIp = Object.keys(defaults)[0];
                if (firstIp && defaults[firstIp].router_id) {
                    globalInput.value = defaults[firstIp].router_id;
                }
            }
        }
    });
}

function _getWorkbenchData() {
    // 如果处于表格模式，先同步到 JSON
    if (workbenchState.varMode === "table") {
        syncVarTableToJson();
    }

    const taskName = document.getElementById("wb-task-name").value.trim();
    const commands = document.getElementById("wb-commands").value;
    const globalVarsStr = document.getElementById("wb-global-vars").value.trim();
    const deviceVarsStr = document.getElementById("wb-device-vars").value.trim();
    const verifyStr = document.getElementById("wb-verify-commands").value.trim();
    const stopOnError = document.getElementById("wb-stop-on-error").checked;
    const saveAfter = document.getElementById("wb-save-after").checked;

    // Get selected devices
    const checkboxes = document.querySelectorAll("#wb-device-select input[type='checkbox']:checked");
    const targetDevices = Array.from(checkboxes).map(cb => cb.value);

    // Parse JSON
    let globalVars = {};
    let deviceVars = {};
    let globalError = null;
    let deviceError = null;

    try {
        if (globalVarsStr && globalVarsStr !== "{}") {
            globalVars = JSON.parse(globalVarsStr);
        }
    } catch (e) {
        globalError = `全局变量 JSON 解析失败: ${e.message}`;
    }

    try {
        if (deviceVarsStr && deviceVarsStr !== "{}") {
            deviceVars = JSON.parse(deviceVarsStr);
        }
    } catch (e) {
        deviceError = `单设备变量 JSON 解析失败: ${e.message}`;
    }

    // Parse verify commands
    const verifyCommands = verifyStr ? verifyStr.split("\n").map(s => s.trim()).filter(Boolean) : [];

    return {
        task_name: taskName,
        target_devices: targetDevices,
        command_template: commands,
        global_vars: globalVars,
        device_vars: deviceVars,
        verify_commands: verifyCommands,
        save_after_success: saveAfter,
        stop_on_error: stopOnError,
        errors: [globalError, deviceError].filter(Boolean),
    };
}

async function workbenchPreview() {
    const data = _getWorkbenchData();

    if (data.errors.length) {
        alert(data.errors.join("\n"));
        return;
    }

    if (!data.target_devices.length) {
        alert("请选择至少一台目标设备");
        return;
    }

    if (!data.command_template.trim()) {
        alert("命令模板不能为空");
        return;
    }

    setButtonLoading("btn-wb-preview", true);
    try {
        const result = await api("/api/config/free-preview", {
            method: "POST",
            body: JSON.stringify({
                task_name: data.task_name,
                target_devices: data.target_devices,
                command_template: data.command_template,
                global_vars: data.global_vars,
                device_vars: data.device_vars,
                verify_commands: data.verify_commands,
            }),
        });

        if (!result.success) {
            const errText = (result.errors || []).join("\n");
            alert("预览失败:\n" + errText);
            return;
        }

        workbenchState.lastPreview = result;
        renderWorkbenchPreview(result);

        // 如果有冲突，禁用下发按钮
        const conflicts = result.conflicts;
        const hasBlockingErrors = conflicts && conflicts.errors && conflicts.errors.length > 0;
        document.getElementById("btn-wb-deploy").disabled = hasBlockingErrors;
    } catch (e) {
        alert("预览请求失败: " + e.message);
    } finally {
        setButtonLoading("btn-wb-preview", false);
    }
}

function renderWorkbenchPreview(result) {
    const area = document.getElementById("wb-preview-area");
    const content = document.getElementById("wb-preview-content");
    const conflictBanner = document.getElementById("wb-conflict-banner");

    // 渲染冲突警告横幅
    renderConflictBanner(result.conflicts);

    // 渲染普通 warnings
    let html = "";
    if (result.warnings && result.warnings.length) {
        html += result.warnings.map(w => `<div class="wb-warn-text">&#9888; ${escapeHtml(w)}</div>`).join("");
    }

    // 并排预览布局
    const preview = result.preview || {};
    const ips = Object.keys(preview);
    const deviceCount = ips.length;

    if (deviceCount === 0) {
        html += '<div class="wb-warn-text">无预览数据</div>';
        content.innerHTML = html;
        area.classList.remove("hidden");
        return;
    }

    // 使用网格布局（最多4列，超出可滚动）
    const cols = Math.min(deviceCount, 4);
    html += `<div class="wb-preview-grid" style="grid-template-columns: repeat(${cols}, 1fr);">`;

    for (const ip of ips) {
        const info = preview[ip];
        const cmds = info.commands || [];
        html += `<div class="wb-device-preview">
            <div class="wb-device-preview-header">
                <span>${escapeHtml(info.device_name || ip)}</span>
                <span style="color:var(--text-muted);font-size:0.8rem">${cmds.length} 条命令</span>
            </div>
            <div class="wb-device-preview-body">
                <pre>${cmds.map(c => `<div class="wb-cmd-line">${escapeHtml(c)}</div>`).join("")}</pre>
            </div>
        </div>`;
    }

    html += "</div>";

    content.innerHTML = html;
    area.classList.remove("hidden");
    document.getElementById("wb-result-area").classList.add("hidden");
}

function renderConflictBanner(conflicts) {
    const banner = document.getElementById("wb-conflict-banner");
    if (!banner) return;

    if (!conflicts || (conflicts.errors?.length === 0 && conflicts.warnings?.length === 0 && conflicts.suggestions?.length === 0)) {
        banner.classList.add("hidden");
        banner.innerHTML = "";
        return;
    }

    let html = "";

    // 严重错误（红色）
    if (conflicts.errors && conflicts.errors.length > 0) {
        html += conflicts.errors.map(e =>
            `<div class="conflict-item conflict-error">
                <span class="conflict-icon">×</span>
                <span class="conflict-text">${escapeHtml(e.message || String(e))}</span>
            </div>`
        ).join("");
    }

    // 警告（黄色）
    if (conflicts.warnings && conflicts.warnings.length > 0) {
        html += conflicts.warnings.map(w =>
            `<div class="conflict-item conflict-warning">
                <span class="conflict-icon">!</span>
                <span class="conflict-text">${escapeHtml(w.message || String(w))}</span>
            </div>`
        ).join("");
    }

    // 建议（蓝色）
    if (conflicts.suggestions && conflicts.suggestions.length > 0) {
        html += conflicts.suggestions.map(s =>
            `<div class="conflict-item conflict-suggestion">
                <span class="conflict-icon">i</span>
                <span class="conflict-text">${escapeHtml(s.message || String(s))}</span>
            </div>`
        ).join("");
    }

    banner.innerHTML = html;
    banner.classList.remove("hidden");
}

async function workbenchDeploy() {
    // Must have preview first
    if (!workbenchState.lastPreview) {
        alert("请先生成预览");
        return;
    }

    const data = _getWorkbenchData();

    if (data.errors.length) {
        alert(data.errors.join("\n"));
        return;
    }

    // Confirmation
    const deviceCount = data.target_devices.length;
    const saveText = data.save_after_success ? "并保存配置" : "";
    const confirmed = await showConfirm(
        "确认下发配置",
        `即将向 ${deviceCount} 台设备下发配置${saveText}。\n系统会先自动备份每台设备的当前配置。\n\n是否确认继续？`
    );
    if (!confirmed) return;

    setButtonLoading("btn-wb-deploy", true);
    try {
        const result = await api("/api/config/free-deploy", {
            method: "POST",
            body: JSON.stringify({
                task_name: data.task_name,
                target_devices: data.target_devices,
                command_template: data.command_template,
                global_vars: data.global_vars,
                device_vars: data.device_vars,
                verify_commands: data.verify_commands,
                save_after_success: data.save_after_success,
                stop_on_error: data.stop_on_error,
            }),
        });

        renderWorkbenchResult(result);
    } catch (e) {
        alert("下发请求失败: " + e.message);
    } finally {
        setButtonLoading("btn-wb-deploy", false);
    }
}

function renderWorkbenchResult(result) {
    const area = document.getElementById("wb-result-area");
    const content = document.getElementById("wb-result-content");

    const summary = result.summary || {};
    const statusClass = result.success ? "success" : (summary.success > 0 ? "partial" : "failed");
    const statusText = result.success ? "全部成功" : (summary.success > 0 ? "部分成功" : "执行失败");

    let html = `<div class="wb-summary-bar ${statusClass}">
        <span>${statusText}</span>
        <span>成功: ${summary.success || 0} / ${summary.total || 0}</span>
        ${result.task_id ? `<span style="font-size:0.75rem;color:var(--text-muted)">任务 ID: ${escapeHtml(result.task_id)}</span>` : ''}
    </div>`;

    // Warnings
    if (result.warnings && result.warnings.length) {
        html += result.warnings.map(w => `<div class="wb-warn-text">&#9888; ${escapeHtml(w)}</div>`).join("");
    }

    // Per-device results
    const results = result.results || {};
    for (const [ip, info] of Object.entries(results)) {
        const success = info.success;
        const headerClass = success ? "success" : "failed";
        const statusIcon = success ? "✓" : "×";
        const errors = info.errors || [];
        const cmdOutput = info.command_output || "";
        const verifyOutputs = info.verify_outputs || {};
        const backupFile = info.backup_file || "";
        const duration = info.duration ? `${info.duration.toFixed(1)}s` : "-";

        html += `<div class="wb-result-device">
            <div class="wb-result-device-header ${headerClass}">
                <span>${statusIcon} ${escapeHtml(info.device_name || ip)}</span>
                <span style="font-size:0.8rem;color:var(--text-muted)">${duration}${backupFile ? ' | 已备份' : ''}</span>
            </div>
            <div class="wb-result-device-body">`;

        if (errors.length) {
            html += `<div class="wb-result-section">
                <div class="wb-result-section-title">错误</div>
                ${errors.map(e => `<div class="wb-error-text">× ${escapeHtml(e)}</div>`).join("")}
            </div>`;
        }

        if (cmdOutput) {
            html += `<div class="wb-result-section">
                <div class="wb-result-section-title">命令回显</div>
                <pre>${escapeHtml(cmdOutput.substring(0, 5000))}</pre>
            </div>`;
        }

        if (Object.keys(verifyOutputs).length) {
            for (const [vcmd, vout] of Object.entries(verifyOutputs)) {
                html += `<div class="wb-result-section">
                    <div class="wb-result-section-title">验证: ${escapeHtml(vcmd)}</div>
                    <pre>${escapeHtml(String(vout).substring(0, 3000))}</pre>
                </div>`;
            }
        }

        html += `</div></div>`;
    }

    content.innerHTML = html;
    area.classList.remove("hidden");
}

async function workbenchSaveScript() {
    const data = _getWorkbenchData();

    if (!data.task_name) {
        const name = prompt("请输入脚本名称:");
        if (!name) return;
        data.task_name = name;
    }

    if (!data.command_template.trim()) {
        alert("命令模板不能为空");
        return;
    }

    try {
        const result = await api("/api/scripts", {
            method: "POST",
            body: JSON.stringify({
                script_name: data.task_name,
                description: "",
                command_template: data.command_template,
                global_vars: data.global_vars,
                sample_device_vars: data.device_vars,
                verify_commands: data.verify_commands,
            }),
        });

        if (result.success) {
            showStatus("batch-status", `脚本 '${data.task_name}' 已保存`, "success");
        } else {
            alert("保存失败: " + (result.message || "未知错误"));
        }
    } catch (e) {
        alert("保存请求失败: " + e.message);
    }
}

async function loadScriptList() {
    const panel = document.getElementById("wb-scripts-panel");
    const list = document.getElementById("wb-scripts-list");

    try {
        const data = await api("/api/scripts");
        const scripts = data.scripts || [];

        if (!scripts.length) {
            list.innerHTML = '<p class="empty-hint">暂无保存的脚本</p>';
        } else {
            list.innerHTML = scripts.map(s => `<div class="wb-script-item" onclick="loadScript('${escapeHtml(s.name)}')">
                <div class="wb-script-item-header">
                    <span class="wb-script-name">${escapeHtml(s.name)}</span>
                    <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();deleteScript('${escapeHtml(s.name)}')">×</button>
                </div>
                <div class="wb-script-desc">${escapeHtml(s.description || "")} | ${escapeHtml(s.updated_at || "")}</div>
            </div>`).join("");
        }

        panel.classList.remove("hidden");
        document.getElementById("wb-tasks-panel").classList.add("hidden");
    } catch (e) {
        alert("加载脚本列表失败: " + e.message);
    }
}

async function loadScript(name) {
    try {
        const data = await api(`/api/scripts/${encodeURIComponent(name)}`);
        if (!data.success) {
            alert("加载失败: " + (data.message || "未知错误"));
            return;
        }

        const s = data.script;
        document.getElementById("wb-task-name").value = s.script_name || "";
        document.getElementById("wb-commands").value = Array.isArray(s.command_template)
            ? s.command_template.join("\n") : (s.command_template || "");
        document.getElementById("wb-global-vars").value = JSON.stringify(s.global_vars || {}, null, 2);
        document.getElementById("wb-device-vars").value = JSON.stringify(s.sample_device_vars || {}, null, 2);
        document.getElementById("wb-verify-commands").value = (s.verify_commands || []).join("\n");

        document.getElementById("wb-scripts-panel").classList.add("hidden");
        showStatus("batch-status", `脚本 '${s.script_name}' 已加载`, "success");
    } catch (e) {
        alert("加载脚本失败: " + e.message);
    }
}

async function deleteScript(name) {
    const confirmed = await showConfirm("删除脚本", `确定删除脚本 '${name}' 吗？`);
    if (!confirmed) return;

    try {
        const result = await api(`/api/scripts/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (result.success) {
            showStatus("batch-status", `脚本 '${name}' 已删除`, "success");
            loadScriptList();
        } else {
            alert("删除失败: " + (result.message || "未知错误"));
        }
    } catch (e) {
        alert("删除请求失败: " + e.message);
    }
}

async function loadTaskHistory() {
    const panel = document.getElementById("wb-tasks-panel");
    const list = document.getElementById("wb-tasks-list");

    try {
        const data = await api("/api/tasks");
        const tasks = data.tasks || [];

        if (!tasks.length) {
            list.innerHTML = '<p class="empty-hint">暂无历史任务记录</p>';
        } else {
            list.innerHTML = tasks.map(t => {
                const status = t.status || "unknown";
                const summary = t.summary || {};
                const devices = (t.target_devices || []).length;
                return `<div class="wb-task-item" onclick="viewTaskDetail('${escapeHtml(t.task_id)}')">
                    <div class="wb-task-item-header">
                        <span class="wb-task-name">${escapeHtml(t.task_name || "未命名")}</span>
                        <span class="wb-task-status ${status}">${status}</span>
                    </div>
                    <div class="wb-task-meta">
                        ${devices} 台设备 | 成功 ${summary.success || 0}/${summary.total || 0} | ${escapeHtml(t.start_time || "")}
                    </div>
                </div>`;
            }).join("");
        }

        panel.classList.remove("hidden");
        document.getElementById("wb-scripts-panel").classList.add("hidden");
    } catch (e) {
        alert("加载历史记录失败: " + e.message);
    }
}

async function viewTaskDetail(taskId) {
    try {
        const data = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
        if (!data.success) {
            alert("加载失败: " + (data.message || "未知错误"));
            return;
        }

        const t = data.task;
        // Render into result area
        const fakeResult = {
            success: t.status === "success",
            task_id: t.task_id,
            task_name: t.task_name,
            summary: t.summary || {},
            results: t.results || {},
            warnings: [],
        };
        renderWorkbenchResult(fakeResult);
        document.getElementById("wb-tasks-panel").classList.add("hidden");
    } catch (e) {
        alert("加载任务详情失败: " + e.message);
    }
}

// ============================================================
// Patches: loadDevices, collectAll
// ============================================================

// Patch loadDevices to also update config device select and workbench
const _origLoadDevices = loadDevices;
loadDevices = async function() {
    await _origLoadDevices();
    updateConfigDeviceSelect();
    updateWorkbenchDeviceSelect();
};

// Patch collectAll to also update config device select and workbench
const _origCollectAll = collectAll;
collectAll = async function(options) {
    await _origCollectAll(options);
    updateConfigDeviceSelect();
    updateWorkbenchDeviceSelect();
};

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initApp, { once: true });
} else {
    initApp();
}

// ============================================================
// 14. 命令目录（声明式表单化配置）
// ============================================================
const catalogState = {
    entries: [],          // 目录条目列表
    currentKey: null,     // 当前选中的指令 key
    probe: null,          // ? 探测结果：{supported:[], unsupported:[], device_type, model, from_cache}
    _probeTimer: null,    // 轮询定时器
};

const DEVICE_TYPE_LABELS = {
    router: "路由器", l3_switch: "三层交换机", switch: "二层交换机",
    firewall: "防火墙", ac: "无线AC", cloud: "云交换机", unknown: "未知",
};

// 页面进入配置下发区时调用：加载目录 + 设备下拉
async function initCatalogPanel() {
    await Promise.all([loadCatalogEntries(), refreshCatalogDeviceSelect(), refreshCatalogProbeSelect()]);
}

// 探测工具栏的设备下拉（与下发区共用设备清单）
async function refreshCatalogProbeSelect() {
    const sel = document.getElementById("catalog-probe-ip");
    if (!sel) return;
    try {
        const devices = await api("/api/devices");
        sel.innerHTML = `<option value="">-- 选择设备 --</option>` +
            (devices || []).map(d =>
                `<option value="${escapeHtml(d.ip)}">${escapeHtml(d.name)} (${escapeHtml(d.ip)})</option>`
            ).join("");
    } catch (e) {
        sel.innerHTML = `<option value="">设备加载失败</option>`;
    }
}

// 探测工具栏：Console / SSH 模式切换（与下发表单区同构）
function onCatalogProbeModeChange() {
    const mode = document.getElementById("catalog-probe-mode").value;
    document.getElementById("catalog-probe-port").classList.toggle("hidden", mode !== "console");
    document.getElementById("catalog-probe-ip").classList.toggle("hidden", mode !== "ssh");
}

async function loadCatalogEntries() {
    try {
        const data = await api("/api/command-catalog");
        catalogState.entries = data.entries || [];
        renderCatalogGrid(data.categories || [], catalogState.entries);
    } catch (e) {
        document.getElementById("catalog-grid").innerHTML =
            `<div class="catalog-loading">加载命令目录失败: ${escapeHtml(e.message)}</div>`;
    }
}

function renderCatalogGrid(categories, entries) {
    const grid = document.getElementById("catalog-grid");
    if (!entries.length) {
        grid.innerHTML = `<div class="catalog-loading">命令目录为空</div>`;
        return;
    }

    // ? 探测结果：支持/不支持集合
    const probe = catalogState.probe;
    const supportedSet = new Set(probe ? probe.supported : []);
    const unsupportedSet = new Set(probe ? probe.unsupported : []);
    const onlySupported = document.getElementById("catalog-only-supported")
        && document.getElementById("catalog-only-supported").checked && probe;

    // 「仅显示支持」过滤
    let viewEntries = entries;
    if (onlySupported) {
        viewEntries = entries.filter(e => supportedSet.has(e.key));
    }

    if (!viewEntries.length) {
        grid.innerHTML = `<div class="catalog-loading">当前设备不支持任何已探测指令（或未探测）</div>`;
        return;
    }

    let html = "";
    for (const cat of categories) {
        const items = viewEntries.filter(e => e.category === cat);
        if (!items.length) continue;
        html += `<div class="catalog-cat-title">${escapeHtml(cat)}</div><div class="catalog-cat-row">`;
        for (const e of items) {
            html += _catalogCardHtml(e, probe, supportedSet, unsupportedSet);
        }
        html += `</div>`;
    }
    const uncat = viewEntries.filter(e => !categories.includes(e.category));
    if (uncat.length) {
        html += `<div class="catalog-cat-row">`;
        for (const e of uncat) {
            html += _catalogCardHtml(e, probe, supportedSet, unsupportedSet);
        }
        html += `</div>`;
    }
    grid.innerHTML = html;
}

// 单张卡片 HTML（含 ? 探测支持徽标）
function _catalogCardHtml(e, probe, supportedSet, unsupportedSet) {
    let badge = "";
    let dimClass = "";
    if (probe) {
        if (supportedSet.has(e.key)) {
            badge = `<span class="catalog-badge catalog-badge-ok" title="设备支持">✔</span>`;
        } else if (unsupportedSet.has(e.key)) {
            badge = `<span class="catalog-badge catalog-badge-no" title="设备不支持">✖</span>`;
            dimClass = " dim";
        }
    }
    return `<button class="catalog-card${dimClass}" onclick="openCatalogForm('${escapeHtml(e.key)}')">
        <span class="catalog-card-icon">${e.icon || "⚙"}</span>
        <span class="catalog-card-name">${escapeHtml(e.name)}</span>
        ${badge}
    </button>`;
}

// 依据当前 catalogState 重新渲染网格（探测结果/过滤变化后调用）
function renderCatalogGridFromState() {
    const cats = [...new Set(catalogState.entries.map(e => e.category))];
    renderCatalogGrid(cats, catalogState.entries);
}

// ? 探测引导：连设备抓命令树，逐条标注「支持/不支持」
async function probeCatalogSupport() {
    const mode = document.getElementById("catalog-probe-mode").value;
    let target;
    if (mode === "console") {
        const port = document.getElementById("catalog-probe-port").value.trim();
        if (!port) { alert("请填写 Console 端口"); return; }
        target = { mode: "console", port: parseInt(port, 10) };
    } else {
        const ip = document.getElementById("catalog-probe-ip").value;
        if (!ip) { alert("请选择目标设备"); return; }
        target = { mode: "ssh", ip };
    }
    const force = document.getElementById("catalog-probe-force").checked;
    const statusEl = document.getElementById("catalog-probe-status");
    setButtonLoading("btn-probe-catalog", true);
    statusEl.textContent = "探测中…";
    statusEl.className = "catalog-probe-status probing";
    try {
        const data = await api("/api/command-catalog/probe", {
            method: "POST",
            body: JSON.stringify({ target, force }),
        });
        if (data.status === "done") {
            _applyProbeResult(data.result, data.from_cache);
        } else if (data.status === "probing") {
            _pollProbe(data.job_id);   // 后台执行，UI 不阻塞
        }
    } catch (e) {
        statusEl.textContent = "探测失败: " + e.message;
        statusEl.className = "catalog-probe-status error";
        setButtonLoading("btn-probe-catalog", false);
    }
}

// 轮询后台探测任务（无感知：UI 持续可用，仅状态栏轻量更新）
function _pollProbe(jobId) {
    const statusEl = document.getElementById("catalog-probe-status");
    if (catalogState._probeTimer) clearInterval(catalogState._probeTimer);
    catalogState._probeTimer = setInterval(async () => {
        try {
            const data = await api(`/api/command-catalog/probe/${jobId}`);
            if (data.status === "done") {
                clearInterval(catalogState._probeTimer);
                catalogState._probeTimer = null;
                _applyProbeResult(data.result, false);
            } else if (data.status === "error") {
                clearInterval(catalogState._probeTimer);
                catalogState._probeTimer = null;
                statusEl.textContent = "探测失败: " + (data.error || "未知错误");
                statusEl.className = "catalog-probe-status error";
                setButtonLoading("btn-probe-catalog", false);
            }
        } catch (e) { /* 轮询异常，等下一次重试 */ }
    }, 800);
}

function _applyProbeResult(result, fromCache) {
    catalogState.probe = result;
    const statusEl = document.getElementById("catalog-probe-status");
    const dt = DEVICE_TYPE_LABELS[result.device_type] || result.device_type || "未知";
    const cacheTag = fromCache ? "（缓存命中，秒回）" : "";
    const errTag = (result.errors && result.errors.length) ? ` ⚠ ${result.errors.join("; ")}` : "";
    statusEl.textContent =
        `已探测 ${dt}${result.model ? " · " + result.model : ""}：` +
        `支持 ${result.supported.length} / 不支持 ${result.unsupported.length}${cacheTag}${errTag}`;
    statusEl.className = "catalog-probe-status done";
    renderCatalogGridFromState();
    setButtonLoading("btn-probe-catalog", false);
}

function openCatalogForm(key) {
    const entry = catalogState.entries.find(e => e.key === key);
    if (!entry) return;
    catalogState.currentKey = key;
    document.getElementById("catalog-grid").classList.add("hidden");
    document.getElementById("catalog-form-area").classList.remove("hidden");
    document.getElementById("catalog-form-title").textContent = entry.name;

    const container = document.getElementById("catalog-form-fields");
    container.innerHTML = (entry.fields || []).map(f => renderFormField(f, null)).join("");

    // 重置预览/结果
    document.getElementById("catalog-preview-area").classList.add("hidden");
    document.getElementById("catalog-result-area").classList.add("hidden");
    onCatalogTargetModeChange();
}

function catalogBackToList() {
    catalogState.currentKey = null;
    document.getElementById("catalog-form-area").classList.add("hidden");
    document.getElementById("catalog-grid").classList.remove("hidden");
}

function onCatalogTargetModeChange() {
    const mode = document.getElementById("catalog-target-mode").value;
    document.getElementById("catalog-console-row").classList.toggle("hidden", mode !== "console");
    document.getElementById("catalog-ssh-row").classList.toggle("hidden", mode !== "ssh");
}

async function refreshCatalogDeviceSelect() {
    const sel = document.getElementById("catalog-target-ip");
    if (!sel) return;
    try {
        const devices = await api("/api/devices");
        sel.innerHTML = `<option value="">-- 请选择设备 --</option>` +
            (devices || []).map(d =>
                `<option value="${escapeHtml(d.ip)}">${escapeHtml(d.name)} (${escapeHtml(d.ip)})</option>`
            ).join("");
    } catch (e) {
        sel.innerHTML = `<option value="">设备加载失败</option>`;
    }
}

function getCatalogFormParams() {
    const container = document.getElementById("catalog-form-fields");
    const inputs = container.querySelectorAll(".protocol-input, .protocol-checkbox");
    const params = {};
    inputs.forEach(input => {
        params[input.name] = input.type === "checkbox" ? input.checked : input.value;
    });
    return params;
}

async function previewCatalogConfig() {
    if (!catalogState.currentKey) { alert("请先选择指令"); return; }
    const params = getCatalogFormParams();
    try {
        const data = await api("/api/command-catalog/render", {
            method: "POST",
            body: JSON.stringify({ key: catalogState.currentKey, params }),
        });
        document.getElementById("catalog-preview-content").textContent =
            (data.commands || []).join("\n");
        document.getElementById("catalog-preview-area").classList.remove("hidden");
    } catch (e) {
        alert("预览失败: " + e.message);
    }
}

async function deployCatalogConfig() {
    if (!catalogState.currentKey) { alert("请先选择指令"); return; }
    const params = getCatalogFormParams();
    const mode = document.getElementById("catalog-target-mode").value;
    const saveConfig = document.getElementById("catalog-save-config").checked;

    const body = { key: catalogState.currentKey, params, save_config: saveConfig };
    if (mode === "console") {
        const port = document.getElementById("catalog-console-port").value.trim();
        if (!port) { alert("请填写 Console 端口"); return; }
        body.console_port = parseInt(port, 10);
    } else {
        const ip = document.getElementById("catalog-target-ip").value;
        if (!ip) { alert("请选择目标设备"); return; }
        body.target_ip = ip;
    }

    const confirmed = await showConfirm(
        "确认下发配置",
        `即将通过 ${mode === "console" ? "Console（零 IP）" : "SSH"} 通道下发指令，是否继续？`
    );
    if (!confirmed) return;

    setButtonLoading("btn-deploy-catalog", true);
    try {
        const data = await api("/api/command-catalog/deploy", {
            method: "POST",
            body: JSON.stringify(body),
        });
        renderDeployResult("catalog-result-area", "catalog-result-content", data);
    } catch (e) {
        renderDeployResult("catalog-result-area", "catalog-result-content", {
            success: false, error: e.message
        });
    } finally {
        setButtonLoading("btn-deploy-catalog", false);
    }
}

// ============================================================
// 15. AI 助教（对话 / 实验指导 / 设置）
// ============================================================

let aiHistory = [];            // [{role, content}] 当前对话历史
let aiStreaming = false;
let aiLabSession = null;
let aiLabExperiments = [];

const AI_QUICK_TASKS = {
    explain_capabilities: "请解读这台设备的能力识别结果：为什么支持/不支持这些协议？运行中、已配置、支持三种状态分别是什么意思？",
    explain_roles: "请讲解这台设备在各协议中扮演的角色（如 Master、根桥、DHCP Server）以及这些角色意味着什么。",
    explain_config: "请像老师一样逐段讲解这台设备的当前配置，说明每一段的作用。",
    diagnose: "",   // 运行时按最近下发结果组装
};

function toggleAIDrawer(open) {
    const drawer = document.getElementById("ai-drawer");
    const backdrop = document.getElementById("ai-drawer-backdrop");
    if (open) {
        drawer.classList.add("open");
        backdrop.classList.remove("hidden");
        updateAIDeviceSelect();
        updateAIPingSelects();
        loadAIStatus();
        if (!aiLabExperiments.length) loadLabExperiments();
        setTimeout(() => { const el = document.getElementById("ai-chat-input"); if (el) el.focus(); }, 250);
    } else {
        drawer.classList.remove("open");
        backdrop.classList.add("hidden");
    }
}

function switchAITab(panelId, btn) {
    document.querySelectorAll(".ai-tab-panel").forEach(p => p.classList.remove("active"));
    document.querySelectorAll(".ai-tab").forEach(t => t.classList.remove("active"));
    const panel = document.getElementById(panelId);
    if (panel) panel.classList.add("active");
    if (btn) btn.classList.add("active");
}

function updateAIDeviceSelect() {
    const select = document.getElementById("ai-device-select");
    if (!select) return;
    const prev = select.value;
    select.innerHTML = '<option value="">-- 不关联设备（通用提问） --</option>';
    for (const d of currentDevices) {
        select.innerHTML += `<option value="${escapeHtml(d.ip)}">${escapeHtml(d.name)} (${escapeHtml(d.ip)})</option>`;
    }
    if (prev && currentDevices.some(d => d.ip === prev)) select.value = prev;
}

function onAIDeviceChanged() { /* 切换设备时上下文由后端按 ip 重新注入 */ }

async function loadAIStatus() {
    try {
        const data = await api("/api/ai/status");
        const badge = document.getElementById("ai-mode-badge");
        badge.textContent = data.mode === "engineer" ? "工程师模式" : "助教模式";
        badge.classList.toggle("running", data.mode === "engineer");
        const status = document.getElementById("ai-settings-status");
        const base = document.getElementById("ai-set-base");
        const model = document.getElementById("ai-set-model");
        const mode = document.getElementById("ai-set-mode");
        if (base && data.api_base && !base.value) base.value = data.api_base;
        if (model && data.model && !model.value) model.value = data.model;
        if (mode) mode.value = data.mode || "tutor";
        if (status) {
            if (data.configured) {
                status.className = "info-bar success";
                status.textContent = `已接入 ${data.model}（Key: ${data.key_masked}），模式: ${data.mode === "engineer" ? "工程师" : "助教"}`;
            } else {
                status.className = "info-bar warning";
                status.textContent = "未配置：请填写 API 地址、Key 和模型名（任意 OpenAI 兼容服务均可）";
            }
        }
    } catch (e) { console.error("AI 状态加载失败:", e.message); }
}

async function saveAISettings() {
    const body = {
        api_base: document.getElementById("ai-set-base").value.trim(),
        api_key: document.getElementById("ai-set-key").value.trim(),
        model: document.getElementById("ai-set-model").value.trim(),
        mode: document.getElementById("ai-set-mode").value,
    };
    try {
        const data = await api("/api/ai/config", { method: "POST", body: JSON.stringify(body) });
        document.getElementById("ai-set-key").value = "";
        showStatus("ai-test-result",
            data.configured ? "配置已保存" : "已保存，但信息不完整，暂无法对话",
            data.configured ? "success" : "warning");
        loadAIStatus();
    } catch (e) {
        showStatus("ai-test-result", `保存失败: ${e.message}`, "error");
    }
}

async function testAIConnection() {
    const el = document.getElementById("ai-test-result");
    el.textContent = "正在测试连接…";
    try {
        const data = await api("/api/ai/test", { method: "POST" });
        el.innerHTML = data.success
            ? `<span class="cap-badge configured">连接成功</span> ${escapeHtml(data.message || "")}`
            : `<span class="cap-badge unsupported">失败</span> ${escapeHtml(data.message || "")}`;
    } catch (e) {
        el.innerHTML = `<span class="cap-badge unsupported">失败</span> ${escapeHtml(e.message)}`;
    }
}

// ---------- 消息渲染 ----------
function _aiRenderMarkdown(text) {
    const escaped = escapeHtml(text);
    const parts = escaped.split(/```(?:\w*\n)?([\s\S]*?)```/g);
    let html = "";
    for (let i = 0; i < parts.length; i++) {
        if (i % 2 === 1) {
            const code = parts[i].replace(/\n$/, "");
            html += `<div class="ai-code-wrap"><pre class="ai-code">${code}</pre>
                <div class="ai-code-actions">
                    <button class="btn btn-sm btn-outline" onclick="copyAIText(this)">复制</button>
                    <button class="btn btn-sm btn-outline" onclick="sendAITextToWorkbench(this)">&rarr; 发送到工作台</button>
                </div></div>`;
        } else {
            html += parts[i]
                .replace(/【模板:([a-zA-Z0-9_]+)】/g, (m, key) =>
                    `<button class="btn btn-sm btn-primary ai-tpl-btn" onclick="aiTemplateJump('${key}')">&#9755; 打开填空模板: ${key}</button>`)
                .replace(/`([^`\n]+)`/g, "<code>$1</code>")
                .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
                .replace(/\n/g, "<br>");
        }
    }
    return html;
}

function aiPushMsg(role, content) {
    const box = document.getElementById("ai-chat-messages");
    if (!box) return document.createElement("div");
    const div = document.createElement("div");
    div.className = `ai-msg ${role}`;
    if (role === "assistant" && content !== undefined) {
        div.innerHTML = _aiRenderMarkdown(content);
    } else if (role === "user") {
        div.innerHTML = escapeHtml(content).replace(/\n/g, "<br>");
    }
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return div;
}

function copyAIText(btn) {
    const pre = btn.closest(".ai-code-wrap")?.querySelector("pre");
    if (!pre) return;
    navigator.clipboard?.writeText(pre.textContent).then(() => {
        btn.textContent = "已复制";
        setTimeout(() => { btn.textContent = "复制"; }, 1500);
    });
}

function sendAITextToWorkbench(btn) {
    const pre = btn.closest(".ai-code-wrap")?.querySelector("pre");
    const ta = document.getElementById("wb-commands");
    if (!pre || !ta) return;
    ta.value = (ta.value ? ta.value.replace(/\s*$/, "\n\n") : "") + pre.textContent.trim() + "\n";
    toggleAIDrawer(false);
    showSection("section-free-workbench");
    showStatus("batch-status", "AI 建议命令已填入配置工作台，请检查变量后预览下发", "success");
}

function aiTemplateJump(templateKey) {
    const ip = document.getElementById("ai-device-select")?.value;
    if (!ip) {
        showStatus("batch-status", "请先在 AI 助教顶部选择设备，再打开填空模板", "warning");
        return;
    }
    toggleAIDrawer(false);
    fillInConfig(ip, templateKey, "");
}

// ---------- 对话主流程 ----------
async function sendAIChat(task, messageText) {
    if (aiStreaming) return;
    const input = document.getElementById("ai-chat-input");
    const message = (messageText !== undefined ? messageText : input.value).trim();
    if (!message) return;
    const ip = document.getElementById("ai-device-select")?.value || "";

    aiPushMsg("user", message);
    if (messageText === undefined) input.value = "";
    aiHistory.push({ role: "user", content: message });

    const msgEl = aiPushMsg("assistant", "…");
    aiStreaming = true;
    const sendBtn = document.getElementById("ai-chat-send");
    if (sendBtn) sendBtn.disabled = true;

    try {
        const reply = await _aiStreamRequest("/api/ai/chat", {
            message, task: task || "free", device_ip: ip || null,
            history: aiHistory.slice(0, -1), stream: true,
        }, msgEl);
        aiHistory.push({ role: "assistant", content: reply });
    } catch (e) {
        msgEl.innerHTML = `<span class="cap-badge unsupported">出错</span> ${escapeHtml(e.message)}
            <br><small>若是未配置，请点右上角"设置"填入 API 地址、Key 与模型名。</small>`;
    } finally {
        aiStreaming = false;
        if (sendBtn) sendBtn.disabled = false;
    }
}

async function _aiStreamRequest(url, body, msgEl, _retried = false) {
    const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Access-Token": _accessToken() },
        body: JSON.stringify(body),
    });
    if (resp.status === 401 && !_retried) {
        await _promptAccessToken();
        return _aiStreamRequest(url, body, msgEl, true);
    }
    if (!resp.ok || !resp.body) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "", reply = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
            const raw = buf.slice(0, idx).trim();
            buf = buf.slice(idx + 2);
            if (!raw.startsWith("data:")) continue;
            let payload;
            try { payload = JSON.parse(raw.slice(5).trim()); } catch (e) { continue; }
            if (payload.error) throw new Error(payload.error);
            if (payload.delta) {
                reply += payload.delta;
                msgEl.textContent = reply;                    // 流式期间显示原文
                const box = msgEl.parentElement;
                if (box) box.scrollTop = box.scrollHeight;
            }
        }
    }
    msgEl.innerHTML = _aiRenderMarkdown(reply || "（空回复）");   // 结束后渲染 markdown
    return reply;
}

function aiQuickTask(task) {
    const ip = document.getElementById("ai-device-select")?.value;
    if (!ip) {
        showStatus("batch-status", "请先在 AI 助教顶部选择一台设备", "warning");
        return;
    }
    if (task === "diagnose") {
        const last = window._aiLastDeploy;
        if (!last || !last.error) {
            sendAIChat("diagnose", "这台设备最近一次配置下发没有记录到错误。如果设备有异常，请把报错原文发给我。");
            return;
        }
        const msg = `请诊断这台设备最近一次配置下发失败的原因。报错信息：\n${last.error}` +
            (last.outputs ? `\n执行回显：\n${last.outputs}` : "");
        sendAIChat("diagnose", msg);
        return;
    }
    sendAIChat(task, AI_QUICK_TASKS[task]);
}

// ---------- 设备侧 ping 连通性验证 ----------
let _aiLastPing = null;

function updateAIPingSelects() {
    const source = document.getElementById("ai-ping-source");
    const target = document.getElementById("ai-ping-target");
    if (!source || !target) return;
    const prevS = source.value, prevT = target.value;
    source.innerHTML = '<option value="">-- 源设备 --</option>';
    target.innerHTML = '<option value="">-- 目标 IP --</option>';
    for (const d of currentDevices) {
        source.innerHTML += `<option value="${escapeHtml(d.ip)}">${escapeHtml(d.name)} (${escapeHtml(d.ip)})</option>`;
        target.innerHTML += `<option value="${escapeHtml(d.ip)}">${escapeHtml(d.ip)}</option>`;
    }
    if (prevS) source.value = prevS;
    if (prevT) target.value = prevT;
}

async function runDevicePing() {
    const src = document.getElementById("ai-ping-source").value;
    const dst = document.getElementById("ai-ping-target").value;
    const resultEl = document.getElementById("ai-ping-result");
    const askBtn = document.getElementById("ai-ping-ask");
    if (!src || !dst) {
        resultEl.innerHTML = '<span class="cap-badge unsupported">请先选择源设备和目标 IP</span>';
        return;
    }
    const runBtn = document.getElementById("ai-ping-run");
    runBtn.disabled = true;
    resultEl.innerHTML = '正在从 ' + escapeHtml(src) + ' ping ' + escapeHtml(dst) + ' …（约 5-10 秒）';
    try {
        const data = await api(`/api/devices/${encodeURIComponent(src)}/device-ping`, {
            method: "POST",
            body: JSON.stringify({ target: dst, count: 4 }),
        });
        _aiLastPing = data;
        const loss = data.loss_rate != null ? data.loss_rate + "%" : "?";
        const avg = data.avg_ms != null ? data.avg_ms + "ms" : "?";
        resultEl.innerHTML =
            (data.success
                ? '<span class="cap-badge configured">连通</span>'
                : '<span class="cap-badge unsupported">不通</span>')
            + ` 丢包 ${loss} · 平均 ${avg}`
            + (data.error ? ` · ${escapeHtml(data.error)}` : "")
            + `<pre>${escapeHtml(data.raw_output || "")}</pre>`;
        askBtn.classList.remove("hidden");
    } catch (e) {
        resultEl.innerHTML = `<span class="cap-badge unsupported">失败</span> ${escapeHtml(e.message)}`;
        askBtn.classList.add("hidden");
    } finally {
        runBtn.disabled = false;
    }
}

function pingResultToTutor() {
    if (!_aiLastPing) return;
    const src = document.getElementById("ai-ping-source")?.value || "";
    const p = _aiLastPing;
    const summary = `我刚在设备 ${src} 上执行了 \`${p.command}\`：`
        + ` 发包 ${p.transmitted}，收包 ${p.received}，`
        + `丢包率 ${p.loss_rate != null ? p.loss_rate + "%" : "未知"}，`
        + `平均时延 ${p.avg_ms != null ? p.avg_ms + "ms" : "未知"}。`
        + (p.success ? "结果是通的。" : "结果不通。")
        + "请结合我的实验目标分析这个结果说明了什么"
        + (p.success ? "" : "，并提示我可能的排查方向");
    if (aiLabSession) {
        document.getElementById("ai-lab-hint-input").value = summary;
        askLabHint();
    } else {
        sendAIChat("diagnose", summary);
    }
}

// ---------- 实验指导（三期） ----------
async function loadLabExperiments() {
    const box = document.getElementById("ai-lab-list");
    try {
        const data = await api("/api/ai/lab/experiments");
        aiLabExperiments = data.experiments || [];
        box.innerHTML = `<p class="ai-lab-intro">选择一个实验开始。进度校验基于"能力识别"的实测数据，AI 助教会按进度给引导提示（不会直接给整段答案）。</p>`
            + aiLabExperiments.map(e => `
                <div class="ai-lab-card">
                    <div class="ai-lab-card-head">
                        <strong>${escapeHtml(e.name)}</strong>
                        <button class="btn btn-sm btn-primary" onclick="startLab('${escapeHtml(e.id)}')">开始</button>
                    </div>
                    <div class="ai-lab-goal">${escapeHtml(e.goal)}</div>
                    <small>${e.check_count} 项校验 · 适用: ${escapeHtml((e.applicable_roles || []).join("/"))}</small>
                </div>`).join("");
    } catch (e) {
        box.innerHTML = `<p class="empty-state">实验目录加载失败: ${escapeHtml(e.message)}</p>`;
    }
}

async function startLab(experimentId) {
    try {
        const data = await api("/api/ai/lab/start", {
            method: "POST",
            body: JSON.stringify({ experiment_id: experimentId, device_ips: [] }),
        });
        aiLabSession = data.session;
        renderLabProgress(data.progress);
        document.getElementById("ai-lab-list").classList.add("hidden");
        document.getElementById("ai-lab-active").classList.remove("hidden");
        const hintBox = document.getElementById("ai-lab-hint-messages");
        hintBox.innerHTML = "";
        aiLabMsg("assistant", `实验「${data.progress.experiment_name}」已开始。目标：${data.progress.goal}。<br>配置过程中随时问我；完成或卡住时点"重新校验"查看进度。`);
    } catch (e) {
        showStatus("batch-status", `开始实验失败: ${e.message}`, "error");
    }
}

function exitLab() {
    aiLabSession = null;
    document.getElementById("ai-lab-active").classList.add("hidden");
    document.getElementById("ai-lab-list").classList.remove("hidden");
}

function renderLabProgress(progress) {
    document.getElementById("ai-lab-name").textContent = progress.experiment_name;
    document.getElementById("ai-lab-goal").textContent = progress.goal;
    document.getElementById("ai-lab-progress-bar").style.width = `${progress.progress || 0}%`;
    document.getElementById("ai-lab-checks").innerHTML = (progress.checks || []).map(c => `
        <div class="ai-lab-check ${c.passed ? 'pass' : 'fail'}">
            <span>${c.passed ? '✓' : '✗'}</span>
            <span class="ai-lab-check-desc">${escapeHtml(c.desc)}</span>
            <small>${c.passed_devices.length}/${c.required} 台: ${escapeHtml(c.passed_devices.join(", ") || "—")}</small>
        </div>`).join("");
}

async function refreshLabProgress() {
    if (!aiLabSession) return;
    try {
        const data = await api(`/api/ai/lab/${aiLabSession.id}/progress`);
        renderLabProgress(data.progress);
        aiLabMsg("assistant", `进度已刷新：${data.progress.progress}%。`
            + (data.progress.progress === 100 ? " 全部校验通过，实验完成！" : " 未完成的项见上方 ✗ 标记，可继续问我。"));
    } catch (e) {
        showStatus("batch-status", `刷新进度失败: ${e.message}`, "error");
    }
}

function aiLabMsg(role, content) {
    const box = document.getElementById("ai-lab-hint-messages");
    if (!box) return document.createElement("div");
    const div = document.createElement("div");
    div.className = `ai-msg ${role}`;
    div.innerHTML = role === "assistant" ? content.replace(/\n/g, "<br>") : escapeHtml(content);
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return div;
}

async function askLabHint() {
    if (!aiLabSession || aiStreaming) return;
    const input = document.getElementById("ai-lab-hint-input");
    const question = input.value.trim();
    if (!question) return;
    input.value = "";
    aiLabMsg("user", question);
    const msgEl = aiLabMsg("assistant", "…");
    aiStreaming = true;
    try {
        await _aiStreamRequest(`/api/ai/lab/${aiLabSession.id}/hint`, {
            question, stream: true,
        }, msgEl);
    } catch (e) {
        msgEl.innerHTML = `<span class="cap-badge unsupported">出错</span> ${escapeHtml(e.message)}`;
    } finally {
        aiStreaming = false;
    }
}

// Enter 发送 / Shift+Enter 换行
document.addEventListener("DOMContentLoaded", () => {
    ["ai-chat-input", "ai-lab-hint-input"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener("keydown", ev => {
            if (ev.key === "Enter" && !ev.shiftKey) {
                ev.preventDefault();
                if (id === "ai-chat-input") sendAIChat();
                else askLabHint();
            }
        });
    });
});

/** 下发失败结果区的一键诊断入口：打开抽屉并自动发起诊断 */
function openAIDiagnose() {
    toggleAIDrawer(true);
    const last = window._aiLastDeploy;
    if (!last || !last.error) return;
    const ip = last.ip || document.getElementById("ai-device-select")?.value || "";
    if (ip) {
        const select = document.getElementById("ai-device-select");
        if (select && currentDevices.some(d => d.ip === ip)) select.value = ip;
    }
    const msg = `请诊断这台设备最近一次配置下发失败的原因。报错信息：\n${last.error}` +
        (last.outputs ? `\n执行回显：\n${last.outputs}` : "");
    setTimeout(() => sendAIChat("diagnose", msg), 300);
}
