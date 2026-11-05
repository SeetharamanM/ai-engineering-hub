"""Discover runnable projects inside the repository.

Each top-level folder of the repository is treated as a project. For every
project we collect:

* title / summary from its README
* runnable entrypoints (Streamlit, Chainlit, Gradio apps and notebooks)
* environment variables it expects (from .env.example, code and README)
* Python dependencies (requirements.txt, pyproject.toml, README pip lines)
* coarse tags used for filtering in the UI
"""

from __future__ import annotations

import hashlib
import re
import shlex
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ipynb_checkpoints", "hub", "resources"}
MAX_DEPTH = 4

APP_KINDS = {
    "streamlit": re.compile(r"^\s*(import streamlit|from streamlit)", re.M),
    "chainlit": re.compile(r"^\s*(import chainlit|from chainlit)", re.M),
    "gradio": re.compile(r"^\s*(import gradio|from gradio)", re.M),
}
# Packages that the launcher itself needs for each kind of entrypoint.
KIND_PACKAGES = {"streamlit": "streamlit", "chainlit": "chainlit", "gradio": "gradio", "notebook": "jupyterlab"}

ENV_CODE_RE = re.compile(
    r"""os\.(?:getenv|environ\.get|environ\.setdefault)\(\s*['"]([A-Z][A-Z0-9_]{2,})['"]"""
    r"""|os\.environ\[\s*['"]([A-Z][A-Z0-9_]{2,})['"]\s*\]"""
)
ENV_README_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:API_KEY|KEY|TOKEN|SECRET|URL|USERNAME|PASSWORD|ID))\b")
ENV_FILE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.M)
# Variables that are referenced but are never things a user should set.
ENV_IGNORE = {"PATH", "HOME", "USER", "PWD", "TOKENIZERS_PARALLELISM", "CUDA_VISIBLE_DEVICES", "PYTHONPATH",
              "PORT", "HOST"}
ENV_IGNORE_PREFIXES = ("YOUR_", "GRADIO_", "STREAMLIT_", "CHAINLIT_", "HF_HUB_", "PYTORCH_")

PIP_LINE_RE = re.compile(r"^\s*(?:!|%)?\s*(?:uv\s+)?pip3?\s+install\s+(.+)$", re.M)

TAG_RULES = [
    ("RAG", r"\brag\b|retrieval|vector (?:db|store|database)|qdrant|milvus|colbert|llamaindex|llama-index"),
    ("Agents", r"crewai|autogen|swarm|\bagents?\b|agentic"),
    ("MCP", r"\bmcp\b|model context protocol"),
    ("OCR", r"\bocr\b"),
    ("Voice", r"voice|audio|speech|assemblyai|cartesia|livekit"),
    ("Multimodal", r"multimodal|multi-modal|vision|image[- ]gen|janus|\bclip\b"),
    ("Local / Ollama", r"ollama|100% local|locally"),
    ("Fine-tuning", r"fine-?tun|distillation|unsloth|\bgrpo\b|siamese"),
    ("Web data", r"firecrawl|bright ?data|browserbase|scrap|linkup|web search"),
    ("Evaluation", r"\beval|observability|opik|cleanlab|trustworthy"),
]

BOILERPLATE_RE = re.compile(r"^-{3,}\s*\n+##\s*📬.*", re.S | re.M)


@dataclass
class Entrypoint:
    id: str
    kind: str  # streamlit | chainlit | gradio | notebook
    path: str  # relative to project root
    label: str


@dataclass
class Project:
    id: str
    name: str
    title: str
    summary: str
    tags: list[str]
    entrypoints: list[Entrypoint]
    env_vars: list[str]
    packages: list[str]
    requirements_files: list[str]
    has_readme: bool
    path: Path = field(repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("path")
        return d


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or hashlib.sha1(name.encode()).hexdigest()[:8]


def _read(path: Path, limit: int = 2_000_000) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _walk(root: Path, depth: int = 0):
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for p in entries:
        if p.name in SKIP_DIRS or p.name.startswith("."):
            continue
        if p.is_dir():
            if depth < MAX_DEPTH:
                yield from _walk(p, depth + 1)
        else:
            yield p


def _find_readme(root: Path) -> Path | None:
    for name in ("README.md", "readme.md", "Readme.md", "README.MD"):
        if (root / name).is_file():
            return root / name
    return None


def strip_boilerplate(md: str) -> str:
    """Remove the newsletter / contribution footer shared by every README."""
    return BOILERPLATE_RE.sub("", md).rstrip() + "\n"


def _title_and_summary(readme: str, fallback: str) -> tuple[str, str]:
    title = fallback
    m = re.search(r"^#\s+(.+)$", readme, re.M)
    if m:
        title = re.sub(r"[*_`]", "", m.group(1)).strip()
    summary = ""
    body = readme[m.end():] if m else readme
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para or para.startswith(("#", "!", "[!", "```", "|", "<", "-", "*", ">")):
            continue
        summary = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", para)
        summary = re.sub(r"[*_`]", "", summary)
        summary = " ".join(summary.split())
        break
    if len(summary) > 280:
        summary = summary[:277].rsplit(" ", 1)[0] + "…"
    return title, summary


def _clean_pip_args(arg_str: str) -> list[str]:
    arg_str = arg_str.split("#", 1)[0]
    arg_str = re.split(r"&&|\|\||;|\|", arg_str, maxsplit=1)[0]
    try:
        tokens = shlex.split(arg_str)
    except ValueError:
        tokens = arg_str.split()
    pkgs = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t in ("-r", "--requirement", "-e", "--editable", "-i", "--index-url", "--extra-index-url", "-c",
                 "--progress-bar", "--target", "-t", "--prefix", "--platform", "--python-version"):
            skip_next = True
            continue
        if t.startswith("-") or t in (".", "\\") or t.endswith(".whl"):
            continue
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~+ ]*$", t) and not t.startswith("git+https://"):
            continue
        pkgs.append(t)
    return pkgs


def _pyproject_deps(path: Path) -> list[str]:
    try:
        data = tomllib.loads(_read(path))
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    deps = data.get("project", {}).get("dependencies", []) or []
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    deps = list(deps) + [k for k in poetry if k.lower() != "python"]
    return [d for d in deps if isinstance(d, str)]


def _pkg_name(spec: str) -> str:
    return re.split(r"[\[<>=!~ ]", spec, maxsplit=1)[0].lower().replace("_", "-")


def scan_project(root: Path) -> Project:
    name = root.name
    pid = _slug(name)
    readme_path = _find_readme(root)
    readme = _read(readme_path) if readme_path else ""
    title, summary = _title_and_summary(readme, name)

    entrypoints: list[Entrypoint] = []
    env: dict[str, None] = {}
    packages: dict[str, str] = {}
    req_files: list[str] = []

    for f in _walk(root):
        rel = f.relative_to(root).as_posix()
        if f.suffix == ".py":
            src = _read(f)
            for kind, rx in APP_KINDS.items():
                if rx.search(src):
                    entrypoints.append(Entrypoint(id=_slug(rel), kind=kind, path=rel, label=rel))
                    break
            for m in ENV_CODE_RE.finditer(src):
                env[m.group(1) or m.group(2)] = None
        elif f.suffix == ".ipynb":
            entrypoints.append(Entrypoint(id=_slug(rel), kind="notebook", path=rel, label=rel))
            src = _read(f)
            for m in ENV_CODE_RE.finditer(src.replace('\\"', '"')):
                env[m.group(1) or m.group(2)] = None
        elif f.name in (".env.example", ".env.sample", ".env.template", "env.example"):
            for m in ENV_FILE_RE.finditer(_read(f)):
                env[m.group(1)] = None
        elif f.name == "requirements.txt":
            req_files.append(rel)
        elif f.name == "pyproject.toml":
            for d in _pyproject_deps(f):
                packages.setdefault(_pkg_name(d), d)

    # .env.example files start with a dot, so _walk skips them; check explicitly.
    for p in root.rglob(".env.*"):
        if p.is_file() and p.suffix in (".example", ".sample", ".template") and ".venv" not in p.parts:
            for m in ENV_FILE_RE.finditer(_read(p)):
                env[m.group(1)] = None

    for m in PIP_LINE_RE.finditer(readme):
        for spec in _clean_pip_args(m.group(1)):
            packages.setdefault(_pkg_name(spec), spec)
    for m in ENV_README_RE.finditer(readme):
        env[m.group(1)] = None

    for e in entrypoints:
        pkg = KIND_PACKAGES[e.kind]
        packages.setdefault(pkg, pkg)

    # Web apps first, then notebooks; app.py first among apps.
    entrypoints.sort(key=lambda e: (e.kind == "notebook", not e.path.endswith("app.py"), e.path.count("/"), e.path))
    # Disambiguate ids.
    seen: dict[str, int] = {}
    for e in entrypoints:
        n = seen.get(e.id, 0)
        seen[e.id] = n + 1
        if n:
            e.id = f"{e.id}-{n}"

    haystack = (name + "\n" + readme).lower()
    tags = [tag for tag, rx in TAG_RULES if re.search(rx, haystack)]

    env_vars = sorted(k for k in env if k not in ENV_IGNORE and not k.startswith(ENV_IGNORE_PREFIXES))
    return Project(
        id=pid,
        name=name,
        title=title,
        summary=summary,
        tags=tags,
        entrypoints=entrypoints,
        env_vars=env_vars,
        packages=list(packages.values()),
        requirements_files=req_files,
        has_readme=readme_path is not None,
        path=root,
    )


def discover(repo_root: Path) -> dict[str, Project]:
    projects: dict[str, Project] = {}
    for d in sorted(repo_root.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
            continue
        p = scan_project(d)
        if not (p.entrypoints or p.has_readme):
            continue
        while p.id in projects:
            p.id += "-x"
        projects[p.id] = p
    return projects
