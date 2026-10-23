import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil

def command(args, timeout=8, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
    if check and result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed (exit {result.returncode})")
    return result.stdout

class System:
    def __init__(self):
        self._tail = {}
        self._tail_time = 0
        self._lock = threading.Lock()
        self._processes = {}
        psutil.cpu_percent()

    def tailscale(self):
        with self._lock:
            if time.monotonic() - self._tail_time < 20:
                return self._tail
            try:
                status = json.loads(command(["tailscale", "status", "--json"]))
                routes = json.loads(command(["tailscale", "serve", "status", "--json"]))
                self._tail = {"status": status.get("BackendState", "Unknown"),
                              "hostname": status.get("Self", {}).get("DNSName", "").rstrip("."),
                              "ips": status.get("TailscaleIPs", []), "routes": routes}
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                self._tail = {"status": "Unavailable", "hostname": "", "ips": [], "routes": {}}
            self._tail_time = time.monotonic()
            return self._tail

    def url_for_ports(self, ports):
        tail = self.tailscale()
        for host, config in tail["routes"].get("Web", {}).items():
            for path, handler in config.get("Handlers", {}).items():
                proxy = urlsplit(handler.get("Proxy", ""))
                if proxy.hostname not in {"localhost", "127.0.0.1", "::1"}:
                    continue
                if any(p["host_port"] == proxy.port for p in ports):
                    host = host.removesuffix(":443")
                    return f"https://{host}{path}"
        if tail["hostname"]:
            for port in ports:
                if port.get("protocol", "tcp") == "tcp" and port.get("host_ip") in {"0.0.0.0", "::", *tail["ips"]}:
                    return f'http://{tail["hostname"]}:{port["host_port"]}'
        return None

    def stats(self):
        memory, disk = psutil.virtual_memory(), psutil.disk_usage("/")
        temperature = None
        try:
            temperature = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except (OSError, ValueError):
            pass
        tail = self.tailscale()
        return {"hostname": socket.gethostname(), "cpu": psutil.cpu_percent(), "cpu_count": psutil.cpu_count(),
                "ram": {"used": memory.used, "total": memory.total, "percent": memory.percent},
                "disk": {"used": disk.used, "total": disk.total, "percent": disk.percent},
                "temperature": temperature, "uptime": time.time() - psutil.boot_time(),
                "tailscale": {k: v for k, v in tail.items() if k != "routes"}}

    def processes(self):
        rows, current = [], {}
        for process in psutil.process_iter(["pid", "name", "username", "memory_info", "create_time"]):
            try:
                key = (process.pid, process.info["create_time"])
                p = self._processes.get(key, process)
                current[key] = p
                rows.append({"pid": p.pid, "command": process.info["name"], "user": process.info["username"],
                             "cpu": p.cpu_percent(), "ram": process.info["memory_info"].rss})
            except (psutil.Error, TypeError):
                continue
        self._processes = current
        return sorted(rows, key=lambda p: (p["cpu"], p["ram"]), reverse=True)

    def listeners(self):
        try:
            return [{"pid": c.pid, "host_ip": c.laddr.ip, "host_port": c.laddr.port, "protocol": "tcp"}
                    for c in psutil.net_connections("tcp") if c.status == "LISTEN"]
        except psutil.Error:
            return []
