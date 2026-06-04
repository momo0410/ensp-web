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
window.__ENSP_APP_LOADED__ = true;

const API_ORIGIN = window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";

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
        info.className = "info-bar " + (auto ? "success" : "info");
        info.title = auto && reason ? "自动识别原因：" + reason : "";
    } catch (e) {
        console.error("选择网卡失败:", e.message);
    }
}

// ============================================================
// 5. 设备列表管理
// ============================================================

async function loadDevices() {
    try {
        const data = await api("/api/devices");
        currentDevices = data.devices;
        renderDevices(data.devices);
    } catch (e) {
        console.error("加载设备列表失败:", e);
    }
}

function renderDevices(devices) {
    const tbody = document.getElementById("devices-tbody");
    if (!devices.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="table-empty">暂无设备，请扫描或手动添加</td></tr>';
        return;
    }

    tbody.innerHTML = devices
        .map((device) => {
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
                        <button class="btn btn-sm btn-outline" onclick="pingDevice('${device.ip}')">Ping</button>
                        <button class="btn btn-sm btn-primary" onclick="collectDevice('${device.ip}')">采集</button>
                        <button class="btn btn-sm btn-danger" onclick="confirmDelete('${device.ip}', '${escapeHtml(device.name)}')">删除</button>
                    </div>
                </td>
            </tr>`;
        })
        .join("");
}

function showAddDevice() {
    document.getElementById("add-device-form").hidden = false;
}

function hideAddDevice() {
    document.getElementById("add-device-form").hidden = true;
}

async function addDevice() {
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
        await api("/api/devices", {
            method: "POST",
            body: JSON.stringify({ name, ip, username, password, device_type }),
        });
        hideAddDevice();
        document.getElementById("add-name").value = "";
        document.getElementById("add-ip").value = "";
        await loadDevices();
    } catch (e) {
        console.error("添加失败:", e.message);
    } finally {
        setButtonLoading("btn-add-device-submit", false);
    }
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
    } catch (e) {
        showStatus("batch-status", `采集 ${ip} 失败: ${e.message}`, "error");
    }
}

async function collectAll() {
    const deviceCount = currentDevices.length;
    if (!deviceCount) {
        console.warn("设备列表为空，请先添加设备");
        return;
    }

    showStatus("batch-status", `正在批量采集 ${deviceCount} 台设备...`, "loading");
    setButtonLoading("btn-collect-all", true);

    try {
        const data = await api("/api/collect-all", { method: "POST" });
        for (const result of data.results) {
            collectionResults[result.ip] = result;
        }
        renderDevices(currentDevices);
        renderDeviceCards(collectionResults);

        const success = data.results.filter((r) => r.ssh_ok).length;
        showStatus(
            "batch-status",
            `批量采集完成: ${success}/${data.results.length} 台设备 SSH 连接成功`,
            success > 0 ? "success" : "error"
        );
    } catch (e) {
        showStatus("batch-status", "批量采集失败: " + e.message, "error");
    } finally {
        setButtonLoading("btn-collect-all", false);
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

// ============================================================
// 7. 设备信息展示（卡片）
// ============================================================

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
                        </div>
                    </div>
                    <div class="device-card-body collapsible">
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

// ============================================================
// 8. 拓扑关系
// ============================================================

async function loadTopology() {
    showStatus("batch-status", "正在加载拓扑信息...", "loading");
    setButtonLoading("btn-refresh-topology", true);

    try {
        const data = await api("/api/topology");
        renderTopology(data.topology.links);
        hideStatus("batch-status");
    } catch (e) {
        showStatus("batch-status", "加载拓扑失败: " + e.message, "error");
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
// 9. 页面初始化
// ============================================================

function initApp() {
    loadInterfaces();
    loadDevices();
    loadCollectionResults();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initApp, { once: true });
} else {
    initApp();
}
