# AI Engineering Hub — Web UI

One local web app to browse, configure and run every project in this repository.

- **Browse** all projects, with search, tag filters (RAG, Agents, MCP, OCR, Voice, Local/Ollama, …) and each project's README.
- **API keys**: set shared keys once (used by every app) or per-project keys (written to `<project>/.env`). Values are masked in the UI and never sent back to the browser in full.
- **Install** a project's dependencies with one click into an isolated `<project>/.venv`. Packages come from `requirements.txt`, `pyproject.toml` and the `pip install` lines in the README.
- **Launch** Streamlit, Chainlit and Gradio apps and Jupyter notebooks on a free port (8601–8699). Running apps are shown embedded in the page, with live logs and a Stop button.

Nothing is hard-coded: projects are discovered by scanning the repository, so new folders show up automatically (restart the hub, or call `POST /api/projects/rescan`).

## Quick start

```bash
cd hub
./run.sh            # Windows: run.bat
```

Open <http://localhost:8600>. The first run creates `hub/.venv` with the hub's three dependencies (FastAPI, Uvicorn, Markdown).

Or run it with your own Python environment:

```bash
pip install -r hub/requirements.txt
cd hub && python -m hub_ui --port 8600
```

### Options

| Flag / env var | Default | Meaning |
| --- | --- | --- |
| `--port` / `HUB_PORT` | `8600` | Port of the hub UI |
| `--host` / `HUB_HOST` | `127.0.0.1` | Interface to bind. Launched apps bind to the same interface. |
| `--token` / `HUB_TOKEN` | none | Require this access token. Generated and printed automatically when binding to a non-loopback address. |
| `--repo` / `HUB_REPO_ROOT` | parent of `hub/` | Repository to scan |

## Typical workflow

1. Click **API keys** (top right) and add the keys you have: `OPENAI_API_KEY`, `FIRECRAWL_API_KEY`, and so on.
2. Pick a project, open **Setup**, and click **Install dependencies**.
3. Open **Run** and click **Launch**. The app appears in the page once it is listening. Use **Open in new tab ↗** for full screen.
4. Many projects run models locally through [Ollama](https://ollama.com). The badge in the header shows whether Ollama is running on `localhost:11434`. Pull the model named in the project's guide first, for example `ollama pull deepseek-r1`.

## Security notes

- By default everything listens on `127.0.0.1` only. The hub starts processes on your machine, so only expose it on a network with `--host 0.0.0.0` together with a token (one is generated if you don't set it).
- Shared keys are stored in `hub/.hub-data/secrets.env` and per-project keys in `<project>/.env`. Both are written with `0600` permissions and ignored by git (see the root `.gitignore`).
- State-changing API calls from other origins are rejected. README HTML is sanitised before it is shown.

## Development

```bash
pip install -r hub/requirements-dev.txt
cd hub && python -m pytest -q
```

Layout:

```
hub/
  hub_ui/
    discovery.py   # scans the repo: entrypoints, env vars, dependencies, tags
    runner.py      # starts and stops processes, assigns ports, captures logs
    envfiles.py    # reads and writes .env files
    server.py      # FastAPI JSON API + static UI
    static/        # single-page UI (plain HTML/CSS/JS, no build step, works offline)
  tests/
```

API docs are available at <http://localhost:8600/api/docs> while the hub is running.
