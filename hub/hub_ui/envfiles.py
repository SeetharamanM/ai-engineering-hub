"""Minimal, dependency-free .env reading/writing."""

from __future__ import annotations

import os
import re
from pathlib import Path

LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
VALID_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        return inner.replace('\\"', '"').replace("\\n", "\n") if v[0] == '"' else inner
    return v.split(" #", 1)[0].rstrip()


def read_env(path: Path) -> dict[str, str]:
    if not path or not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = LINE_RE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def _quote(v: str) -> str:
    if v == "" or re.search(r"[\s#'\"\\=$]", v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return v


def update_env(path: Path, changes: dict[str, str | None]) -> dict[str, str]:
    """Apply changes to a .env file, preserving comments and ordering.

    A value of ``None`` or ``""`` removes the key.
    """
    for k in changes:
        if not VALID_KEY.match(k):
            raise ValueError(f"Invalid variable name: {k!r}")
        if changes[k] is not None and ("\n" in changes[k] or "\r" in changes[k]):
            raise ValueError(f"Value for {k} must be a single line")
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(changes)
    new_lines = []
    for line in lines:
        m = LINE_RE.match(line) if not line.lstrip().startswith("#") else None
        if m and m.group(1) in pending:
            val = pending.pop(m.group(1))
            if val:
                new_lines.append(f"{m.group(1)}={_quote(val)}")
            continue
        new_lines.append(line)
    for k, v in pending.items():
        if v:
            new_lines.append(f"{k}={_quote(v)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return read_env(path)


def effective_env(project_dir: Path, global_env_file: Path | None) -> dict[str, str]:
    """Environment for a launched app: system < hub-wide secrets < project .env."""
    env = dict(os.environ)
    if global_env_file:
        env.update(read_env(global_env_file))
    env.update(read_env(project_dir / ".env"))
    return env


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:3] + "•" * 6 + value[-4:]
