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
async function api(path, options = {}) {
    const config = {
        headers: { "Content-Type": "application/json" },
        ...options,
    };
    const url = path.startsWith("http") ? path : API_ORIGIN + path;
    const resp = await fetch(url, config);
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
        runAutoDiscoveryAndCollection();
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
        await api(path, {
            method,
            body: JSON.stringify({ name, ip, username, password, device_type }),
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
            updateConfigDeviceSelect();
        }
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

// 协议表单模板定义
const PROTOCOL_FORMS = {
    ospf: {
        title: "OSPF 配置",
        icon: "&#x1F310;",
        fields: [
            { name: "process_id", label: "进程号", type: "number", default: "1", required: true, placeholder: "1" },
            { name: "area_id", label: "区域 ID", type: "text", default: "0.0.0.0", required: true, placeholder: "0.0.0.0" },
            { name: "network", label: "网络地址", type: "text", required: true, placeholder: "192.168.1.0" },
            { name: "wildcard", label: "反掩码", type: "text", required: true, placeholder: "0.0.0.255" },
            { name: "auth_type", label: "认证方式", type: "select", options: [
                { value: "none", label: "无认证" },
                { value: "simple", label: "明文认证" },
                { value: "md5", label: "MD5 认证" }
            ], default: "none" },
        ]
    },
    bgp: {
        title: "BGP 配置",
        icon: "&#x1F30D;",
        fields: [
            { name: "local_as", label: "本地 AS 号", type: "number", required: true, placeholder: "65001" },
            { name: "router_id", label: "Router ID", type: "text", placeholder: "1.1.1.1" },
            { name: "neighbor_ip", label: "邻居 IP", type: "text", placeholder: "192.168.1.2" },
            { name: "neighbor_as", label: "邻居 AS 号", type: "number", placeholder: "65002" },
            { name: "network", label: "宣告网络", type: "text", placeholder: "10.0.0.0" },
        ]
    },
    ipsec: {
        title: "IPSec VPN 配置",
        icon: "&#x1F512;",
        fields: [
            { name: "ike_proposal", label: "IKE 提议号", type: "number", default: "1", placeholder: "1" },
            { name: "encryption", label: "加密算法", type: "select", options: [
                { value: "aes-128", label: "AES-128" },
                { value: "aes-256", label: "AES-256" },
                { value: "3des", label: "3DES" }
            ], default: "aes-128" },
            { name: "dh_group", label: "DH 组", type: "select", options: [
                { value: "2", label: "Group 2" },
                { value: "5", label: "Group 5" },
                { value: "14", label: "Group 14" }
            ], default: "2" },
            { name: "pre_share_key", label: "预共享密钥", type: "password", required: true, placeholder: "输入预共享密钥" },
            { name: "acl_number", label: "ACL 编号", type: "number", default: "3000", placeholder: "3000" },
            { name: "local_subnet", label: "本地子网", type: "text", required: true, placeholder: "192.168.1.0 0.0.0.255" },
            { name: "remote_subnet", label: "远端子网", type: "text", required: true, placeholder: "192.168.2.0 0.0.0.255" },
            { name: "remote_ip", label: "远端 IP", type: "text", required: true, placeholder: "203.0.113.1" },
        ]
    },
    rip: {
        title: "RIP 配置",
        icon: "&#x1F4CD;",
        fields: [
            { name: "version", label: "版本", type: "select", options: [
                { value: "1", label: "RIPv1" },
                { value: "2", label: "RIPv2" }
            ], default: "2" },
            { name: "network", label: "网络地址", type: "text", required: true, placeholder: "192.168.1.0" },
        ]
    },
    isis: {
        title: "IS-IS 配置",
        icon: "&#x1F517;",
        fields: [
            { name: "process_id", label: "进程号", type: "text", default: "1", placeholder: "1" },
            { name: "net_entity", label: "网络实体", type: "text", required: true, placeholder: "49.0001.0010.0100.1001.00" },
            { name: "level", label: "级别", type: "select", options: [
                { value: "level-1", label: "Level-1" },
                { value: "level-2", label: "Level-2" },
                { value: "level-1-2", label: "Level-1-2" }
            ], default: "level-2" },
        ]
    },
    static: {
        title: "静态路由配置",
        icon: "&#x1F6E4;",
        fields: [
            { name: "dest_network", label: "目标网络", type: "text", required: true, placeholder: "10.0.0.0" },
            { name: "mask", label: "子网掩码", type: "text", required: true, placeholder: "255.255.255.0" },
            { name: "next_hop", label: "下一跳", type: "text", required: true, placeholder: "192.168.1.1" },
        ]
    },
    nat: {
        title: "NAT 配置",
        icon: "&#x1F310;",
        fields: [
            { name: "nat_type", label: "NAT 类型", type: "select", options: [
                { value: "easyip", label: "Easy IP" },
                { value: "nat-pool", label: "NAT Pool" }
            ], default: "easyip", required: true },
            { name: "inside_interface", label: "内网接口", type: "text", required: true, placeholder: "GigabitEthernet0/0/1" },
            { name: "outside_interface", label: "外网接口", type: "text", required: true, placeholder: "GigabitEthernet0/0/0" },
            { name: "acl_number", label: "ACL 编号", type: "number", placeholder: "3000" },
        ]
    },
    dhcp: {
        title: "DHCP 配置",
        icon: "&#x1F4CA;",
        fields: [
            { name: "pool_name", label: "地址池名称", type: "text", required: true, placeholder: "pool1" },
            { name: "network", label: "网络地址", type: "text", required: true, placeholder: "192.168.1.0" },
            { name: "mask", label: "子网掩码", type: "text", required: true, placeholder: "255.255.255.0" },
            { name: "gateway", label: "网关地址", type: "text", required: true, placeholder: "192.168.1.1" },
            { name: "dns", label: "DNS 服务器", type: "text", placeholder: "8.8.8.8" },
            { name: "lease_days", label: "租期（天）", type: "number", default: "1", placeholder: "1" },
        ]
    },
    acl: {
        title: "ACL 配置",
        icon: "&#x1F4CB;",
        fields: [
            { name: "acl_number", label: "ACL 编号", type: "number", required: true, placeholder: "3000" },
            { name: "action", label: "动作", type: "select", options: [
                { value: "permit", label: "允许" },
                { value: "deny", label: "拒绝" }
            ], required: true },
            { name: "source", label: "源地址", type: "text", required: true, placeholder: "192.168.1.0" },
            { name: "source_wildcard", label: "源反掩码", type: "text", placeholder: "0.0.0.255" },
            { name: "dest", label: "目标地址", type: "text", placeholder: "10.0.0.0" },
        ]
    },
    ntp: {
        title: "NTP 配置",
        icon: "&#x1F570;",
        fields: [
            { name: "server_ip", label: "NTP 服务器", type: "text", required: true, placeholder: "129.6.15.28" },
        ]
    },
    snmp: {
        title: "SNMP 配置",
        icon: "&#x1F4E1;",
        fields: [
            { name: "version", label: "版本", type: "select", options: [
                { value: "v2c", label: "SNMPv2c" },
                { value: "v3", label: "SNMPv3" }
            ], default: "v2c" },
            { name: "community", label: "团体名", type: "text", required: true, placeholder: "public" },
            { name: "trap_target", label: "Trap 目标 IP", type: "text", placeholder: "192.168.1.100" },
        ]
    },
};

// 协议配置全局状态
let protocolConfigState = {
    deviceIp: null,
    deviceIndex: null,
    protocols: [],
    configSections: {},
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
        selectedProtocol: null,
        configMode: "form",
    };
}

/**
 * 加载设备协议配置
 */
async function loadProtocolConfig(ip) {
    try {
        const data = await api(`/api/devices/${encodeURIComponent(ip)}/protocols`);
        protocolConfigState.protocols = data.protocols || [];
        protocolConfigState.configSections = data.config_sections || {};

        // 渲染已检测到的协议
        renderDetectedProtocols();

        // 渲染协议标签页
        renderProtocolTabs();
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
        .map(p => `<span class="badge badge-success">${escapeHtml(PROTOCOL_NAMES[p] || p)}</span>`)
        .join(" ");
}

/**
 * 渲染协议选择标签页
 */
function renderProtocolTabs() {
    const container = document.getElementById("protocol-tabs");
    const allProtocols = Object.keys(PROTOCOL_FORMS);
    const configuredProtocols = protocolConfigState.protocols;

    container.innerHTML = allProtocols
        .map(p => {
            const isConfigured = configuredProtocols.includes(p);
            const form = PROTOCOL_FORMS[p];
            const isActive = protocolConfigState.selectedProtocol === p;
            return `<button class="protocol-tab ${isActive ? "active" : ""} ${isConfigured ? "configured" : ""}"
                        data-protocol="${p}"
                        onclick="selectProtocol('${p}')">
                        <span class="protocol-tab-icon">${form.icon || "&#x1F4E6;"}</span>
                        <span class="protocol-tab-name">${escapeHtml(form.title)}</span>
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
    const formConfig = PROTOCOL_FORMS[protocol];
    if (!formConfig) return;

    const container = document.getElementById("protocol-form-config");
    const currentConfig = protocolConfigState.configSections[protocol];
    const existingParams = currentConfig ? parseConfigToParams(protocol, currentConfig.config || "") : {};

    let html = `<div class="protocol-form">
        <h4 class="protocol-form-title">${formConfig.icon || ""} ${escapeHtml(formConfig.title)}</h4>
        <div class="protocol-form-fields">`;

    for (const field of formConfig.fields) {
        const value = existingParams[field.name] || field.default || "";
        const requiredMark = field.required ? '<span class="required">*</span>' : "";

        html += `<div class="form-field">
            <label>${escapeHtml(field.label)}${requiredMark}</label>`;

        if (field.type === "select") {
            html += `<select name="${field.name}" class="protocol-input">
                ${field.options.map(opt =>
                    `<option value="${opt.value}" ${opt.value === value ? "selected" : ""}>${escapeHtml(opt.label)}</option>`
                ).join("")}
            </select>`;
        } else if (field.type === "checkbox") {
            html += `<input type="checkbox" name="${field.name}" ${value ? "checked" : ""} class="protocol-checkbox">`;
        } else {
            html += `<input type="${field.type}" name="${field.name}" value="${escapeHtml(value)}"
                placeholder="${escapeHtml(field.placeholder || "")}"
                ${field.required ? "required" : ""}
                class="protocol-input">`;
        }

        html += `</div>`;
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
};

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
            return `<div class="capability-item" title="${escapeHtml(cap.display_cmd || '')}">
                <span class="capability-name">${escapeHtml(CAPABILITY_NAMES[key] || key)}</span>
                <span class="capability-badges">${badges}</span>
            </div>`;
        }).join("");

        const errors = (r.errors || []).length
            ? `<div class="info-bar error" style="margin-top:8px">错误: ${escapeHtml(r.errors.join("; "))}</div>`
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
                </div>
            </div>
            <div class="capability-card-body collapsible">
                <div class="capability-grid">${capItems}</div>
                ${errors}
            </div>
        </div>`;
    }).join("");
}

// ============================================================
// 12. 设备配置下发
// ============================================================

let configDeployState = {
    selectedDeviceIp: null,
    selectedConfigType: null,
    templates: {},
};

function updateConfigDeviceSelect() {
    const select = document.getElementById("config-device-select");
    if (!select) return;
    select.innerHTML = '<option value="">-- 请先选择设备 --</option>';
    for (const device of currentDevices) {
        const cap = capabilityResults[device.ip];
        const role = cap ? (ROLE_NAMES[cap.device_role] || "") : "";
        const label = role ? `${device.name} (${device.ip}) - ${role}` : `${device.name} (${device.ip})`;
        select.innerHTML += `<option value="${escapeHtml(device.ip)}">${escapeHtml(label)}</option>`;
    }
}

async function onConfigDeviceSelected() {
    const select = document.getElementById("config-device-select");
    const ip = select.value;
    configDeployState.selectedDeviceIp = ip;
    configDeployState.selectedConfigType = null;

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

function renderConfigTemplates(templates, capabilities) {
    const grid = document.getElementById("config-templates-grid");
    const caps = capabilities ? capabilities.capabilities : {};

    // Group templates by category
    const categories = {};
    const categoryNames = {
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
    const categoryOrder = ["routing", "switching", "network", "security", "vpn", "other"];
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
                <span class="tpl-icon">${tpl.icon || "&#x1F4E6;"}</span>
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

    const container = document.getElementById("config-form-fields");
    document.getElementById("config-form-title").textContent = tpl.name;

    container.innerHTML = tpl.fields.map(field => {
        const requiredMark = field.required ? '<span class="required">*</span>' : '';
        let input = '';
        if (field.type === 'select') {
            input = `<select name="${field.name}" class="protocol-input">
                ${field.options.map(opt => `<option value="${opt.value}" ${opt.value === (field.default || '') ? 'selected' : ''}>${escapeHtml(opt.label)}</option>`).join('')}
            </select>`;
        } else if (field.type === 'checkbox') {
            input = `<input type="checkbox" name="${field.name}" ${field.default ? 'checked' : ''} class="protocol-checkbox" style="width:18px;height:18px">`;
        } else {
            input = `<input type="${field.type}" name="${field.name}"
                value="${escapeHtml(field.default || '')}"
                placeholder="${escapeHtml(field.placeholder || '')}"
                ${field.required ? 'required' : ''}
                class="protocol-input">`;
        }
        return `<div class="form-row">
            <label>${escapeHtml(field.label)}${requiredMark}</label>
            ${input}
        </div>`;
    }).join("");

    document.getElementById("config-form-area").classList.remove("hidden");
    document.getElementById("config-preview-area").classList.add("hidden");
    document.getElementById("config-result-area").classList.add("hidden");
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

function renderDeployResult(areaId, contentId, data) {
    const area = document.getElementById(areaId);
    const content = document.getElementById(contentId);

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
    loadInterfaces();
    loadDevices();
    loadCollectionResults();
    loadCapabilityResults();
    updateWorkbenchDeviceSelect();
}

// ============================================================
// 16. 自由配置工作台
// ============================================================

let workbenchState = {
    lastPreview: null,
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

function workbenchDeviceChanged() {
    // Clear preview/result when device selection changes
    document.getElementById("wb-preview-area").classList.add("hidden");
    document.getElementById("wb-result-area").classList.add("hidden");
    workbenchState.lastPreview = null;
}

function _getWorkbenchData() {
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
    } catch (e) {
        alert("预览请求失败: " + e.message);
    } finally {
        setButtonLoading("btn-wb-preview", false);
    }
}

function renderWorkbenchPreview(result) {
    const area = document.getElementById("wb-preview-area");
    const content = document.getElementById("wb-preview-content");

    let html = "";

    // Warnings
    if (result.warnings && result.warnings.length) {
        html += result.warnings.map(w => `<div class="wb-warn-text">&#9888; ${escapeHtml(w)}</div>`).join("");
    }

    // Per-device preview
    const preview = result.preview || {};
    for (const [ip, info] of Object.entries(preview)) {
        const cmds = info.commands || [];
        html += `<div class="wb-device-preview">
            <div class="wb-device-preview-header">
                <span>&#x1F4E6; ${escapeHtml(info.device_name || ip)}</span>
                <span style="color:var(--text-muted);font-size:0.8rem">${cmds.length} 条命令</span>
            </div>
            <div class="wb-device-preview-body">
                <pre>${cmds.map(c => `<div class="wb-cmd-line">${escapeHtml(c)}</div>`).join("")}</pre>
            </div>
        </div>`;
    }

    content.innerHTML = html;
    area.classList.remove("hidden");
    document.getElementById("wb-result-area").classList.add("hidden");
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
        const statusIcon = success ? "&#x2705;" : "&#x274C;";
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
                ${errors.map(e => `<div class="wb-error-text">&#x274C; ${escapeHtml(e)}</div>`).join("")}
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
                    <span class="wb-script-name">&#x1F4C4; ${escapeHtml(s.name)}</span>
                    <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();deleteScript('${escapeHtml(s.name)}')">&#x1F5D1;</button>
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
