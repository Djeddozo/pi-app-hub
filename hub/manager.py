import hashlib
import json
import secrets
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import HELPER
from .discovery import DockerProvider, SystemdProvider, UserSystemdProvider, ManualProvider
from .security import docker_id, protected, redact, service_name
from .system import command

class ManifestEvents(FileSystemEventHandler):
    def __init__(self, signal):
        self.signal = signal

    def on_any_event(self, event):
        if event.event_type in {"created", "modified", "deleted", "moved"}:
            self.signal.set()

class Manager:
    def __init__(self, registry, system, manifests):
        self.registry, self.system = registry, system
        self.docker = DockerProvider(system)
        self.providers = [self.docker, SystemdProvider(system), UserSystemdProvider(system), ManualProvider(manifests)]
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.signal = threading.Event()
        self.metrics = {}
        self.health = {}
        self.last_scan = None
        self.previews = {}
        self.manifests = manifests
        self.observer = None
        self.threads = []

    def scan(self):
        with self.lock:
            counts = {"new": 0, "updated": 0, "missing": 0}
            for provider in self.providers:
                try:
                    result = self.registry.reconcile(provider.source, provider.discover())
                    for key in counts:
                        counts[key] += result[key]
                    self.health[provider.source] = {"ok": True, "message": "Connected"}
                except Exception as exc:
                    self.health[provider.source] = {"ok": False, "message": f"Discovery unavailable ({type(exc).__name__}); keeping last known apps"}
            self.last_scan = time.time()
            return {**counts, "providers": self.health.copy(), "at": self.last_scan}

    def apps(self):
        apps = self.registry.list()
        for app in apps:
            app["discovery_ok"] = self.health.get(app["source"], {}).get("ok", False)
            if app["source"] == "docker":
                sample = self.metrics.get(app["id"], {})
                app.update(cpu=sample.get("cpu"), ram=sample.get("ram"), memory_source=sample.get("memory_source"))
            if app["status"] == "Missing":
                app.update(cpu=None, ram=None)
            elif app["status"] == "Running":
                app["uptime"] += max(0, time.time() - app["last_seen"])
            app["open_url"] = app.get("web_url") if "web_url" in app["edited_fields"] and app.get("web_url") else app.get("tailscale_url") or app.get("web_url")
            app["manageable"] = not app["protected"] and app["discovery_ok"] and app["status"] != "Missing" and app["runtime"] != "manual"
        return apps

    def start(self):
        self.manifests.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.observer = Observer()
        self.observer.schedule(ManifestEvents(self.signal), str(self.manifests))
        self.observer.start()
        for target in (self.discovery_loop, self.events_loop, self.metrics_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        self.signal.set()
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
        for thread in self.threads:
            thread.join(timeout=1)

    def discovery_loop(self):
        while not self.stop.is_set():
            self.scan()
            self.signal.wait(60)
            self.signal.clear()
            self.stop.wait(0.4)  # coalesce event bursts

    def events_loop(self):
        while not self.stop.is_set():
            try:
                with self.docker.connect().events(decode=True, filters={"type": "container"}, until=int(time.time()) + 45) as events:
                    for event in events:
                        if self.stop.is_set():
                            break
                        if event.get("Action", "").split(":")[0] in {"create", "start", "stop", "die", "destroy", "restart", "health_status", "rename"}:
                            self.signal.set()
            except Exception:
                self.stop.wait(10)

    def metrics_loop(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            while not self.stop.is_set():
                apps = [a for a in self.registry.list() if a["source"] == "docker" and a["status"] != "Missing"]
                def sample(app):
                    try:
                        return app["id"], self.docker.metrics(app)
                    except Exception:
                        return app["id"], {"cpu": None, "ram": None}
                for app_id, metrics in pool.map(sample, apps):
                    self.metrics[app_id] = metrics
                self.stop.wait(5)

    def fresh(self, app_id):
        app = self.registry.get(app_id)
        if not app:
            raise ValueError("Application not found")
        provider = next(p for p in self.providers if p.source == app["source"])
        snapshots = provider.discover()  # Never execute against a stale registry snapshot.
        self.registry.reconcile(provider.source, snapshots)
        app = self.registry.get(app_id)
        if not app or app["status"] == "Missing":
            raise ValueError("Application is missing; scan again")
        if app["protected"] or protected(app["key"].split(":", 1)[-1], *[m["name"] for m in app["members"]]):
            raise ValueError("This is a protected component")
        return app

    @staticmethod
    def ordered_members(app):
        pending = list(app["members"])
        ordered, done = [], set()
        roles = {m.get("role") for m in pending}
        while pending:
            ready = [m for m in pending if not ({d.split(":")[0] for d in m.get("depends_on", "").split(",")} & roles - done)]
            if not ready:
                raise ValueError("Cyclic Compose dependencies; manage this project with Compose")
            for m in ready:
                ordered.append(m)
                done.add(m.get("role"))
                pending.remove(m)
        return ordered

    def action(self, app_id, action):
        if action not in {"start", "stop", "restart"}:
            raise ValueError("Unsupported action")
        with self.lock:
            app = self.fresh(app_id)
            if app["runtime"] == "manual":
                raise ValueError("Manual registrations are observation-only; use a systemd service for lifecycle management")
            if app["source"] == "docker":
                members = self.ordered_members(app)
                if action in {"stop", "restart"}:
                    for member in reversed(members):
                        self.docker.connect().containers.get(docker_id(member["id"])).stop(timeout=10)
                if action in {"start", "restart"}:
                    for member in members:
                        self.docker.connect().containers.get(docker_id(member["id"])).start()
            else:
                unit = service_name(app["members"][0]["id"])
                if app.get("scope") == "user":
                    import os
                    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus"}
                    result = subprocess.run(["systemctl", "--user", action, unit], capture_output=True, text=True, timeout=90, env=env, check=False)
                    if result.returncode:
                        raise RuntimeError("user systemd action failed")
                else:
                    command(["sudo", "-n", HELPER, action, unit], timeout=90)
            self.signal.set()

    def logs(self, app_id, lines):
        app = self.registry.get(app_id)
        if not app:
            raise ValueError("Application not found")
        if app["source"] == "docker":
            logs = []
            for member in app["members"]:
                content = self.docker.connect().containers.get(docker_id(member["id"])).logs(tail=lines, timestamps=True)
                logs.append(f'--- {member["name"]} ---\n' + content[-150000:].decode("utf-8", errors="replace"))
            return redact("\n".join(logs)[-300000:])
        if app["source"] in {"systemd", "systemd-user"}:
            return redact(command(["journalctl", "--no-pager", "--output=short-iso", "-n", str(lines), "-u", service_name(app["members"][0]["id"])] )[-300000:])
        return "Manual registrations do not have a managed log source."

    @staticmethod
    def fingerprint(app):
        fields = {k: app.get(k) for k in ("id", "name", "protected", "project_path", "data_directories")}
        fields["members"] = sorted(m["id"] for m in app["members"])
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

    def preview(self, app_id):
        with self.lock:
            app = self.fresh(app_id)
            removals, warnings = [], []
            unit_digest = None
            if app["source"] == "docker":
                removals = [f'Container {m["name"]} ({m["id"]})' for m in app["members"]]
                warnings = ["Containers will be stopped and removed. Their writable container layers are lost. Volumes, bind-mounted data, images, networks and project files are preserved."]
            elif app["source"] == "systemd":
                unit = service_name(app["members"][0]["id"])
                # Helper validates the exact source, symlinks, ownership and dependent units before approval.
                unit_digest = command(["sudo", "-n", HELPER, "check", unit]).strip()
                removals = [f"Stop and disable {unit}", f"Archive /etc/systemd/system/{unit} in /var/lib/pi-app-hub/unit-backups/"]
                warnings = ["Application files, configuration and data are preserved. Unit drop-ins remain in place. The service can be restored from its archived unit file."]
            elif app["source"] == "systemd-user":
                raise ValueError("User systemd units can be started, stopped or restarted, but are not removed by Pi App Hub")
            else:
                removals = ["Dashboard registration only"]
                warnings = ["The running process, manifest and all files are preserved. This registration will be hidden from future scans."]
            token = secrets.token_urlsafe(32)
            self.previews = {k: v for k, v in self.previews.items() if v["expires"] > time.time()}
            self.previews[token] = {"app_id": app_id, "fingerprint": self.fingerprint(app), "expires": time.time() + 120, "unit_digest": unit_digest}
            return {"token": token, "name": app["name"], "remove": removals, "preserve": app["data_directories"],
                    "project_path": app.get("project_path"), "warnings": warnings, "expires_in": 120}

    def uninstall(self, app_id, token, confirmation):
        with self.lock:
            preview = self.previews.pop(token, None)
            if not preview or preview["app_id"] != app_id or preview["expires"] < time.time():
                raise ValueError("Preview expired or invalid; review a fresh uninstall preview")
            app = self.fresh(app_id)
            if confirmation != app["name"] or self.fingerprint(app) != preview["fingerprint"]:
                raise ValueError("Confirmation does not match or application changed; review again")
            if app["source"] == "docker":
                for member in reversed(self.ordered_members(app)):
                    container = self.docker.connect().containers.get(docker_id(member["id"]))
                    container.stop(timeout=10)
                    container.remove(v=False, force=False)
            elif app["source"] == "systemd":
                command(["sudo", "-n", HELPER, "uninstall", service_name(app["members"][0]["id"]), preview["unit_digest"]], timeout=90)
            elif app["source"] == "systemd-user":
                raise ValueError("User systemd units are preserved; remove them from their user manager")
            self.registry.remove(app_id)
            self.signal.set()
