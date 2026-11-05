"""Process management: launching apps, installing dependencies, capturing logs."""

from __future__ import annotations

import collections
import itertools
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import Entrypoint, Project
from .envfiles import effective_env

LOG_LINES = 5000
PORT_RANGE = range(8601, 8700)


def project_python(project: Project) -> str:
    """The interpreter used for a project: its own .venv if present, else the hub's."""
    for cand in (project.path / ".venv" / "bin" / "python", project.path / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    return sys.executable


def _port_free(port: int, host: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host if host not in ("", "0.0.0.0") else "0.0.0.0", port))
            return True
        except OSError:
            return False


def _port_open(port: int, host: str) -> bool:
    target = "127.0.0.1" if host in ("", "0.0.0.0") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((target, port)) == 0


@dataclass
class Process:
    id: str
    kind: str  # "app" | "install"
    project_id: str
    label: str
    commands: list[list[str]]
    cwd: str
    env: dict[str, str] = field(repr=False)
    entrypoint_id: str | None = None
    app_kind: str | None = None
    port: int | None = None
    url_path: str = "/"
    status: str = "starting"  # starting | running | exited | failed | stopped
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    _popen: subprocess.Popen | None = field(default=None, repr=False)
    _lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LOG_LINES), repr=False)
    _seq: itertools.count = field(default_factory=itertools.count, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stop_requested: bool = field(default=False, repr=False)

    def log(self, text: str) -> None:
        with self._lock:
            for line in text.rstrip("\n").split("\n"):
                self._lines.append((next(self._seq), line))

    def logs_after(self, after: int) -> tuple[list[dict], int]:
        with self._lock:
            out = [{"seq": s, "text": t} for s, t in self._lines if s > after]
            last = self._lines[-1][0] if self._lines else after
        return out, last

    @property
    def alive(self) -> bool:
        return self.status in ("starting", "running")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "project_id": self.project_id,
            "entrypoint_id": self.entrypoint_id,
            "app_kind": self.app_kind,
            "label": self.label,
            "port": self.port,
            "url_path": self.url_path,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "command": " ".join(self.commands[-1]) if self.commands else "",
        }


class ProcessManager:
    def __init__(self, app_host: str = "127.0.0.1", global_env_file: Path | None = None):
        self.app_host = app_host
        self.global_env_file = global_env_file
        self._procs: dict[str, Process] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- queries
    def list(self) -> list[Process]:
        with self._lock:
            return sorted(self._procs.values(), key=lambda p: p.started_at, reverse=True)

    def get(self, pid: str) -> Process | None:
        return self._procs.get(pid)

    def active_for(self, project_id: str) -> list[Process]:
        return [p for p in self.list() if p.project_id == project_id and p.alive]

    # ----------------------------------------------------------------- actions
    def _alloc_port(self) -> int:
        used = {p.port for p in self._procs.values() if p.alive and p.port}
        for port in PORT_RANGE:
            if port not in used and _port_free(port, self.app_host):
                return port
        raise RuntimeError("No free port available in range %d-%d" % (PORT_RANGE.start, PORT_RANGE.stop - 1))

    def launch(self, project: Project, ep: Entrypoint) -> Process:
        with self._lock:
            for p in self._procs.values():
                if p.project_id == project.id and p.entrypoint_id == ep.id and p.alive:
                    return p
            port = self._alloc_port()
            py = project_python(project)
            env = effective_env(project.path, self.global_env_file)
            env["PYTHONUNBUFFERED"] = "1"
            host = self.app_host
            file_path = project.path / ep.path
            cwd = file_path.parent
            fname = file_path.name
            url_path = "/"
            if ep.kind == "streamlit":
                cmd = [py, "-m", "streamlit", "run", fname,
                       "--server.port", str(port), "--server.address", host,
                       "--server.headless", "true", "--browser.gatherUsageStats", "false"]
            elif ep.kind == "chainlit":
                cmd = [py, "-m", "chainlit", "run", fname, "--port", str(port), "--host", host, "--headless"]
            elif ep.kind == "gradio":
                env["GRADIO_SERVER_PORT"] = str(port)
                env["GRADIO_SERVER_NAME"] = host
                cmd = [py, fname]
            elif ep.kind == "notebook":
                token = secrets.token_urlsafe(16)
                cwd = project.path
                cmd = [py, "-m", "jupyter", "lab", "--no-browser", f"--port={port}", f"--ip={host}",
                       f"--ServerApp.token={token}", f"--ServerApp.root_dir={project.path}",
                       "--ServerApp.port_retries=0"]
                url_path = f"/lab/tree/{ep.path}?token={token}"
            else:  # pragma: no cover - discovery only emits the kinds above
                raise ValueError(f"Unsupported entrypoint kind {ep.kind}")

            proc = Process(
                id=uuid.uuid4().hex[:12], kind="app", project_id=project.id, label=ep.label,
                commands=[cmd], cwd=str(cwd), env=env, entrypoint_id=ep.id, app_kind=ep.kind,
                port=port, url_path=url_path,
            )
            self._procs[proc.id] = proc
        threading.Thread(target=self._run, args=(proc,), daemon=True).start()
        return proc

    def install(self, project: Project, use_venv: bool = True) -> Process:
        with self._lock:
            for p in self._procs.values():
                if p.project_id == project.id and p.kind == "install" and p.alive:
                    return p
            commands: list[list[str]] = []
            venv_dir = project.path / ".venv"
            if use_venv:
                if not venv_dir.exists():
                    commands.append([sys.executable, "-m", "venv", str(venv_dir)])
                py = str(venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
                commands.append([py, "-m", "pip", "install", "--upgrade", "pip"])
            else:
                py = sys.executable
            pip_args: list[str] = []
            for req in project.requirements_files:
                pip_args += ["-r", str(project.path / req)]
            pip_args += project.packages
            if pip_args:
                commands.append([py, "-m", "pip", "install", *pip_args])
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            proc = Process(
                id=uuid.uuid4().hex[:12], kind="install", project_id=project.id,
                label="Install dependencies" + (" (.venv)" if use_venv else " (hub interpreter)"),
                commands=commands, cwd=str(project.path), env=env,
            )
            self._procs[proc.id] = proc
        threading.Thread(target=self._run, args=(proc,), daemon=True).start()
        return proc

    def stop(self, pid: str, timeout: float = 8.0) -> Process | None:
        proc = self._procs.get(pid)
        if not proc:
            return None
        proc._stop_requested = True
        popen = proc._popen
        if popen and popen.poll() is None:
            proc.log("[hub] stopping…")
            try:
                if os.name == "nt":
                    popen.terminate()
                else:
                    os.killpg(popen.pid, signal.SIGTERM)
                popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    popen.kill()
                else:
                    os.killpg(popen.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return proc

    def remove(self, pid: str) -> bool:
        proc = self._procs.get(pid)
        if not proc or proc.alive:
            return False
        with self._lock:
            self._procs.pop(pid, None)
        return True

    def shutdown(self) -> None:
        for p in self.list():
            if p.alive:
                self.stop(p.id, timeout=3)

    # ---------------------------------------------------------------- internals
    def _run(self, proc: Process) -> None:
        code = 0
        try:
            for cmd in proc.commands:
                if proc._stop_requested:
                    break
                proc.log("$ " + " ".join(cmd))
                popen = subprocess.Popen(
                    cmd, cwd=proc.cwd, env=proc.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, bufsize=1, errors="replace",
                    start_new_session=(os.name != "nt"),
                )
                proc._popen = popen
                if proc.kind == "app":
                    threading.Thread(target=self._watch_ready, args=(proc, popen), daemon=True).start()
                else:
                    proc.status = "running"
                assert popen.stdout is not None
                for line in popen.stdout:
                    proc.log(line)
                code = popen.wait()
                if code != 0:
                    break
        except FileNotFoundError as e:
            proc.log(f"[hub] could not start: {e}")
            code = 127
        except Exception as e:  # noqa: BLE001 - report anything to the UI log
            proc.log(f"[hub] error: {e!r}")
            code = 1
        proc.exit_code = code
        proc.ended_at = time.time()
        if proc._stop_requested:
            proc.status = "stopped"
        elif code == 0:
            proc.status = "exited"
        else:
            proc.status = "failed"
        proc.log(f"[hub] process {proc.status} (exit code {code})")

    def _watch_ready(self, proc: Process, popen: subprocess.Popen) -> None:
        while popen.poll() is None and proc.status == "starting":
            if proc.port and _port_open(proc.port, self.app_host):
                proc.status = "running"
                proc.log(f"[hub] app is listening on port {proc.port}")
                return
            time.sleep(0.5)
