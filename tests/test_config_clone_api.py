from fastapi.testclient import TestClient
import time

import main
import routes_config

FAKE_DEVICES = [
    {"ip": "192.168.56.11", "name": "AR1"},
    {"ip": "192.168.56.12", "name": "AR2"},
]


def _client():
    return TestClient(main.app)


SOURCE = """
#
 sysname AR1
#
interface GigabitEthernet0/0/0
 ip address 192.168.56.11 255.255.255.0
#
ospf 1 router-id 1.1.1.1
 area 0
  network 192.168.56.0 0.0.0.255
#
return
"""

TARGET = """
#
 sysname AR2
#
interface GigabitEthernet0/0/0
 ip address 192.168.56.12 255.255.255.0
#
return
"""


def test_clone_preview_api_builds_and_caches_deployable_plan(monkeypatch):
    def fake_load(device, refresh=True):
        config = SOURCE if device["ip"] == "192.168.56.11" else TARGET
        return {
            "success": True,
            "config": config,
            "backup_file": f"{device['name']}.cfg",
            "source": "test",
            "errors": [],
        }

    monkeypatch.setattr(routes_config, "_load_config_for_clone", fake_load)
    monkeypatch.setattr(routes_config, "load_devices", lambda: FAKE_DEVICES)
    main.state["clone_previews"] = {}
    client = _client()
    response = client.post(
        "/api/config/clone/preview",
        json={
            "source_device": "192.168.56.11",
            "target_devices": ["192.168.56.12"],
            "domains": ["identity", "interfaces", "routing"],
            "auto_resolve": True,
            "refresh_config": False,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["preview_id"] in main.state["clone_previews"]
    commands = data["targets"]["192.168.56.12"]["commands"]
    assert "ospf 1 router-id 192.168.56.12" in commands
    assert "ip address 192.168.56.12 255.255.255.0" not in commands


def test_clone_deploy_requires_confirmed_cached_preview(monkeypatch):
    main.state["clone_previews"] = {
        "preview123": {
            "created_at": time.time(),
            "source_device": "192.168.56.11",
            "target_devices": ["192.168.56.12"],
            "targets": {
                "192.168.56.12": {
                    "device_name": "AR2",
                    "commands": ["lldp enable"],
                }
            },
            "conflicts": {"errors": []},
        }
    }

    monkeypatch.setattr(
        routes_config,
        "execute_free_deploy",
        lambda **kwargs: {
            "success": True,
            "results": {
                "192.168.56.12": {
                    "success": True,
                    "errors": [],
                    "device_name": "AR2",
                }
            },
            "summary": {"total": 1, "success": 1, "failed": 0},
        },
    )
    monkeypatch.setattr(routes_config, "save_task_record", lambda record: None)
    client = _client()

    denied = client.post(
        "/api/config/clone/deploy",
        json={"preview_id": "preview123", "confirm": False},
    )
    assert denied.status_code == 400

    allowed = client.post(
        "/api/config/clone/deploy",
        json={"preview_id": "preview123", "confirm": True},
    )
    assert allowed.status_code == 200
    assert allowed.json()["summary"]["success"] == 1
