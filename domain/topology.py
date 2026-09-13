"""
拓扑生成：把采集结果里的 LLDP 邻居关系整理成节点与链路。
"""

from typing import List

def generate_topology(devices: List[dict], collection_results: dict) -> dict:
    """
    根据所有设备的 LLDP 邻居信息生成拓扑关系。
    自动去重，并尝试匹配对端设备 IP。
    """
    links = []
    seen_links = set()

    for device in devices:
        ip = device["ip"]
        result = collection_results.get(ip, {})
        info = result.get("info", {})
        lldp_neighbors = info.get("lldp_neighbors", [])
        hostname = info.get("hostname") or device.get("name", ip)

        # 建立设备名称到 IP 的映射
        name_to_ip = {}
        for d in devices:
            d_result = collection_results.get(d["ip"], {})
            d_info = d_result.get("info", {})
            d_name = d_info.get("hostname") or d.get("name", "")
            if d_name:
                name_to_ip[d_name] = d["ip"]
            name_to_ip[d.get("name", "")] = d["ip"]

        for neighbor in lldp_neighbors:
            remote_dev = neighbor.get("remote_device", "")
            local_intf = neighbor.get("local_interface", "")
            remote_intf = neighbor.get("remote_interface", "")
            remote_mgmt_ip = neighbor.get("remote_mgmt_ip", "")
            if not remote_dev:
                continue

            # 去重
            link_key = tuple(sorted([hostname, remote_dev])) + (local_intf,)
            if link_key in seen_links:
                continue
            seen_links.add(link_key)

            # 优先用名称映射的 IP，其次用 LLDP 管理 IP
            target_ip = name_to_ip.get(remote_dev, "") or remote_mgmt_ip

            links.append({
                "source": hostname,
                "source_ip": ip,
                "target": remote_dev,
                "target_ip": target_ip,
                "local_interface": local_intf,
                "remote_interface": remote_intf,
            })

    return {"links": links}
