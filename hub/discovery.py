"""Discovery providers return snapshots. A failed provider never marks its apps missing."""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .security import docker_id, protected, safe_path, safe_url, service_name
from .system import command

def title(value):
    return value.replace("-", " ").replace("_", " ").title()

def icon_for(name):
    value = name.lower()
    for words, icon in [(('finance', 'vault', 'bank'), 'wallet'), (('uptime', 'monitor'), 'activity'),
                        (('padel', 'sport'), 'calendar'), (('car',), 'car'), (('bot', 'chat', 'companion'), 'message'),
                        (('hub',), 'grid'), (('database', 'redis', 'postgres'), 'database')]:
        if any(word in value for word in words):
            return icon
    return 'box'

def aggregate(states):
    if any(s in {"failed", "dead", "unhealthy", "restarting"} for s in states):
        return "Failed"
    if states and all(s in {"running", "active"} for s in states):
        return "Running"
    if any(s in {"running", "active"} for s in states):
        return "Failed"  # a partially running project needs attention
    return "Stopped"

def counter(value):
    try:
        result = int(value)
        return result if 0 <= result < 2**64 - 1 else None
    except (TypeError, ValueError):
        return None

class DockerProvider:
    source = "docker"

    def __init__(self, system):
        self.system = system
        self.client = None
        self.samples = {}

    def connect(self):
        if self.client is None:
            # Never accept an arbitrary remote Docker host from an environment variable.
            self.client = docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=8)
        return self.client

    def discover(self):
        groups = {}
        for c in self.connect().containers.list(all=True):
            attrs, labels = c.attrs, c.labels or {}
            project = labels.get("com.docker.compose.project")
            key = f"compose:{project}" if project else f"docker:{c.id}"
            name = labels.get("pi-app-hub.name") or title(project or c.name)
            app = groups.setdefault(key, {"key": key, "source": self.source, "runtime": "Docker Compose" if project else "Docker",
                "name": name, "icon": icon_for(name), "members": [], "ports": [], "data_directories": [],
                "project_path": None, "protected": False, "health_check": None, "web_url": None})
            app["protected"] |= protected(c.name, attrs.get("Config", {}).get("Image"), project) or labels.get("pi-app-hub.protected") == "true"
            path = labels.get("com.docker.compose.project.working_dir")
            if path:
                try:
                    app["project_path"] = safe_path(path)
                except ValueError:
                    pass
            state = attrs.get("State", {})
            health = state.get("Health", {}).get("Status")
            started = state.get("StartedAt", "")
            uptime = 0
            if state.get("Running") and started and not started.startswith("0001"):
                uptime = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds())
            app["members"].append({"id": docker_id(c.id), "name": c.name,
                "state": "unhealthy" if health == "unhealthy" else ("failed" if state.get("ExitCode", 0) and not state.get("Running") else c.status),
                "health": health, "uptime": uptime, "role": labels.get("com.docker.compose.service", c.name),
                "depends_on": labels.get("com.docker.compose.depends_on", "")})
            for container_port, bindings in (attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
                for binding in bindings or []:
                    app["ports"].append({"container_port": container_port, "host_port": int(binding["HostPort"]),
                                         "host_ip": binding["HostIp"], "protocol": container_port.split("/")[-1]})
            for mount in attrs.get("Mounts", []):
                app["data_directories"].append({"type": mount["Type"], "path": mount.get("Source"), "target": mount.get("Destination")})
            if health:
                app["health_check"] = "Docker health check: " + health
            if labels.get("pi-app-hub.url"):
                try:
                    app["web_url"] = safe_url(labels["pi-app-hub.url"])
                except ValueError:
                    pass
        for app in groups.values():
            app["members"].sort(key=lambda x: x["id"])
            app["status"] = aggregate([m["state"] for m in app["members"]])
            app["uptime"] = min((m["uptime"] for m in app["members"]), default=0)
            app["tailscale_url"] = self.system.url_for_ports(app["ports"])
        return list(groups.values())

    def metrics(self, app):
        cpu, ram, memory_source = 0., 0, "Docker working set"
        for member in app["members"]:
            if member["state"] not in {"running", "unhealthy"}:
                continue
            container = self.connect().containers.get(docker_id(member["id"]))
            data = container.stats(stream=False, one_shot=True)
            current = data.get("cpu_stats", {})
            previous = self.samples.get(member["id"], current)
            self.samples[member["id"]] = current
            delta = current.get("cpu_usage", {}).get("total_usage", 0) - previous.get("cpu_usage", {}).get("total_usage", 0)
            system_delta = current.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
            if system_delta > 0:
                cpu += delta / system_delta * current.get("online_cpus", 1) * 100
            memory = data.get("memory_stats", {})
            if "usage" in memory:
                ram += max(0, memory["usage"] - memory.get("stats", {}).get("inactive_file", 0))
            else:
                # Some Pi kernels disable memory cgroup accounting. Sum process RSS instead.
                memory_source = "Process RSS (memory cgroup accounting unavailable)"
                top = container.top(ps_args="-eo pid")
                for row in top.get("Processes", []):
                    try:
                        ram += psutil.Process(int(row[0])).memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
        return {"cpu": round(cpu, 1), "ram": ram, "memory_source": memory_source}

class SystemdProvider:
    source = "systemd"
    scope = "system"
    unit_dir = Path("/etc/systemd/system")

    def __init__(self, system):
        self.system = system
        self.samples = {}

    def units(self):
        # Excludes distro units, aliases and template instances. No process heuristics.
        return [p for p in self.unit_dir.glob("*.service") if p.is_file() and not p.is_symlink() and "@" not in p.name]

    def show(self, units, properties):
        return command(["systemctl", "show", *[service_name(p.name) for p in units], "--property=" + properties])

    def discover(self):
        units = self.units()
        if not units:
            return []
        properties = "Id,Description,ActiveState,SubState,MainPID,WorkingDirectory,FragmentPath,ControlGroup,MemoryCurrent,CPUUsageNSec,ActiveEnterTimestampMonotonic"
        output = self.show(units, properties)
        listeners, apps = self.system.listeners(), []
        for block in output.strip().split("\n\n"):
            info = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            unit = info.get("Id", "")
            if unit not in {p.name for p in units}:
                continue
            pid = int(info.get("MainPID", 0))
            pids = {pid}
            if pid:
                try:
                    pids.update(p.pid for p in psutil.Process(pid).children(recursive=True))
                except psutil.Error:
                    pass
            ports = [{k: v for k, v in c.items() if k != "pid"} for c in listeners if c["pid"] in pids and c["pid"]]
            name = info.get("Description") or title(unit.removesuffix(".service"))
            now = time.monotonic()
            usage = counter(info.get("CPUUsageNSec")) or 0
            previous = self.samples.get(unit, (now, usage))
            cpu = max(0, (usage - previous[1]) / 1e9 / (now - previous[0]) * 100) if now > previous[0] else 0
            self.samples[unit] = (now, usage)
            ram = counter(info.get("MemoryCurrent"))
            if ram is None:
                ram = sum(self.process_ram(p) for p in pids if p)
            uptime = max(0, time.clock_gettime(time.CLOCK_BOOTTIME) - int(info.get("ActiveEnterTimestampMonotonic", 0)) / 1e6) if info.get("ActiveState") == "active" else 0
            try:
                path = safe_path(info.get("WorkingDirectory"))
            except ValueError:
                path = None
            apps.append({"key": f"{self.scope}:{unit}", "source": self.source, "scope": self.scope, "runtime": "systemd", "name": name,
                "icon": icon_for(name), "status": aggregate([info.get("ActiveState")]), "protected": protected(unit, name),
                "members": [{"id": unit, "name": unit, "state": info.get("ActiveState"), "pid": pid}],
                "ports": ports, "project_path": path, "data_directories": [], "health_check": None,
                "web_url": None, "tailscale_url": self.system.url_for_ports(ports), "uptime": uptime,
                "cpu": round(cpu, 1), "ram": ram})
        return apps

    @staticmethod
    def process_ram(pid):
        try:
            return psutil.Process(pid).memory_info().rss
        except psutil.Error:
            return 0

class UserSystemdProvider(SystemdProvider):
    """Explicit user service files only; user processes are never inferred as applications."""
    source = "systemd-user"
    scope = "user"
    unit_dir = Path.home() / ".config/systemd/user"

    def show(self, units, properties):
        uid = os.getuid()
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
               "XDG_RUNTIME_DIR": f"/run/user/{uid}", "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus"}
        result = subprocess.run(["systemctl", "--user", "show", *[service_name(p.name) for p in units], "--property=" + properties],
                                capture_output=True, text=True, timeout=12, env=env, check=False)
        if result.returncode:
            raise RuntimeError("user systemd manager unavailable")
        return result.stdout

class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    icon: str = Field(default="box", pattern=r"^(box|wallet|activity|calendar|car|message|database|grid|globe|code)$")
    project_path: str | None = None
    web_url: str | None = None
    health_check: str | None = Field(default=None, max_length=200)
    data_directories: list[str] = Field(default_factory=list, max_length=30)
    protected: bool = False
    pid: int | None = Field(default=None, gt=0)
    process_started_at: float | None = None

    _path = field_validator("project_path")(safe_path)
    _url = field_validator("web_url")(safe_url)

    @field_validator("data_directories")
    @classmethod
    def validate_directories(cls, values):
        return [safe_path(v) for v in values]

class ManualProvider:
    source = "manual"

    def __init__(self, directory):
        self.directory = directory
        self.processes = {}

    def discover(self):
        apps = []
        for path in self.directory.glob("*.json"):
            if path.is_symlink() or path.stat().st_size > 32768:
                raise ValueError("Manual manifests must be regular JSON files smaller than 32 KiB")
            manifest = Manifest.model_validate_json(path.read_text())
            app = manifest.model_dump()
            app.update(key=f"manual:{manifest.id}", source=self.source, runtime="manual", status="Stopped", members=[],
                       ports=[], tailscale_url=None, uptime=0, cpu=0, ram=0)
            app["protected"] |= protected(manifest.id, manifest.name)
            app["data_directories"] = [{"type": "directory", "path": d} for d in manifest.data_directories]
            if manifest.pid and manifest.process_started_at:
                try:
                    process = self.processes.get(manifest.pid) or psutil.Process(manifest.pid)
                    if abs(process.create_time() - manifest.process_started_at) < 0.01 and process.is_running():
                        self.processes[manifest.pid] = process
                        app.update(status="Running", cpu=process.cpu_percent(), ram=process.memory_info().rss,
                                   uptime=time.time() - process.create_time(), members=[{"id": str(process.pid), "name": process.name(), "state": "running"}])
                except psutil.Error:
                    pass
            app.pop("id")
            apps.append(app)
        if len({a["key"] for a in apps}) != len(apps):
            raise ValueError("Manual manifest IDs must be unique")
        return apps
