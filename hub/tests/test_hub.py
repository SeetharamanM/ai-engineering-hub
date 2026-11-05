import json
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub_ui.discovery import _clean_pip_args, discover, strip_boilerplate
from hub_ui.envfiles import read_env, update_env
from hub_ui.server import Settings, create_app

REPO = Path(__file__).resolve().parents[2]

FAKE_APP = textwrap.dedent('''
    """Pretends to be a Gradio app: serves HTTP on GRADIO_SERVER_PORT."""
    import os, http.server
    try:
        import gradio  # noqa: F401  (marks this file as a gradio entrypoint)
    except ImportError:
        pass
    key = os.getenv("DEMO_API_KEY", "")
    print("key-length", len(key), flush=True)
    srv = http.server.HTTPServer((os.environ["GRADIO_SERVER_NAME"], int(os.environ["GRADIO_SERVER_PORT"])),
                                 http.server.SimpleHTTPRequestHandler)
    srv.serve_forever()
''')


@pytest.fixture()
def fake_repo(tmp_path):
    proj = tmp_path / "demo-project"
    proj.mkdir()
    (proj / "README.md").write_text(textwrap.dedent("""
        # Demo Project

        A tiny **demo** used in tests. Uses [Ollama](https://ollama.com) locally.

        ```bash
        pip install requests "pydantic==2.10.1" --upgrade
        ```

        ![shot](shot.png)

        <script>alert(1)</script>
        <img src="x.png" onerror="alert(1)">

        ---

        ## 📬 Stay Updated with Our Newsletter!
        boilerplate
    """))
    (proj / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (proj / "app.py").write_text(FAKE_APP)
    (proj / ".env.example").write_text("DEMO_API_KEY=\nOTHER_TOKEN=\n")
    (proj / "nb.ipynb").write_text(json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
    (tmp_path / "empty-dir").mkdir()
    return tmp_path


@pytest.fixture()
def client(fake_repo, tmp_path):
    app = create_app(Settings(repo_root=fake_repo, data_dir=tmp_path / "data"))
    with TestClient(app) as c:
        yield c


def test_discovery_fake(fake_repo):
    projects = discover(fake_repo)
    assert list(projects) == ["demo-project"]
    p = projects["demo-project"]
    assert p.title == "Demo Project"
    assert p.summary.startswith("A tiny demo used in tests")
    assert [e.kind for e in p.entrypoints] == ["gradio", "notebook"]
    assert p.env_vars == ["DEMO_API_KEY", "OTHER_TOKEN"]
    assert "requests" in p.packages and "pydantic==2.10.1" in p.packages and "gradio" in p.packages
    assert "Local / Ollama" in p.tags


def test_discovery_real_repo():
    projects = discover(REPO)
    assert len(projects) > 30
    assert "hub" not in projects
    assert any(e.kind == "streamlit" for p in projects.values() for e in p.entrypoints)
    assert "FIRECRAWL_API_KEY" in projects["agentic-rag"].env_vars


def test_pip_arg_cleaning():
    assert _clean_pip_args("-q --progress-bar off foo bar[x]>=1 && echo hi") == ["foo", "bar[x]>=1"]
    assert _clean_pip_args("-r requirements.txt pkg ./local.whl") == ["pkg"]


def test_strip_boilerplate():
    md = "# T\n\nbody\n\n---\n\n## 📬 Stay Updated\nnews\n"
    assert "Stay Updated" not in strip_boilerplate(md)


def test_env_roundtrip(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\nA=1\nB='two words'\n")
    assert read_env(f) == {"A": "1", "B": "two words"}
    update_env(f, {"A": None, "C": 'has "quotes" and spaces'})
    assert read_env(f) == {"B": "two words", "C": 'has "quotes" and spaces'}
    assert f.read_text().startswith("# comment")
    with pytest.raises(ValueError):
        update_env(f, {"bad-name": "x"})
    with pytest.raises(ValueError):
        update_env(f, {"OK": "line\nbreak"})


def test_api_projects_and_readme(client):
    data = client.get("/api/projects").json()
    assert data[0]["id"] == "demo-project"
    assert data[0]["env_missing"] == ["DEMO_API_KEY", "OTHER_TOKEN"]
    d = client.get("/api/projects/demo-project").json()
    html = d["readme_html"]
    assert "<script" not in html and "onerror" not in html
    assert "Stay Updated" not in html
    assert "/api/projects/demo-project/files/shot.png" in html
    assert client.get("/api/projects/demo-project/files/shot.png").status_code == 200
    assert client.get("/api/projects/demo-project/files/app.py").status_code == 404
    assert client.get("/api/projects/demo-project/files/../../etc/passwd").status_code == 404
    assert client.get("/").status_code == 200


def test_api_env(client, fake_repo):
    r = client.put("/api/projects/demo-project/env", json={"values": {"DEMO_API_KEY": "sk-secret-123456"}})
    assert r.status_code == 200
    row = next(x for x in r.json() if x["name"] == "DEMO_API_KEY")
    assert row["set"] and row["source"] == "project" and "secret" not in row["preview"]
    assert read_env(fake_repo / "demo-project" / ".env")["DEMO_API_KEY"] == "sk-secret-123456"
    client.put("/api/settings/env", json={"values": {"OTHER_TOKEN": "tok-global-abcdef"}})
    rows = client.get("/api/projects/demo-project/env").json()
    assert next(x for x in rows if x["name"] == "OTHER_TOKEN")["source"] == "global"
    assert client.get("/api/projects").json()[0]["env_missing"] == []
    assert client.put("/api/settings/env", json={"values": {"bad name": "x"}}).status_code == 400


def test_cross_origin_post_refused(client):
    r = client.post("/api/projects/rescan", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_launch_and_stop(client):
    client.put("/api/projects/demo-project/env", json={"values": {"DEMO_API_KEY": "abcd"}})
    proc = client.post("/api/projects/demo-project/run", json={"entrypoint": "app-py"}).json()
    assert proc["port"] and proc["status"] in ("starting", "running")
    # Launching again returns the same process.
    assert client.post("/api/projects/demo-project/run", json={"entrypoint": "app-py"}).json()["id"] == proc["id"]
    deadline = time.time() + 15
    while time.time() < deadline:
        p = client.get(f"/api/processes/{proc['id']}").json()
        if p["status"] == "running":
            break
        time.sleep(0.2)
    assert p["status"] == "running"
    with urllib.request.urlopen(f"http://127.0.0.1:{proc['port']}/", timeout=5) as resp:
        assert resp.status == 200
    logs = client.get(f"/api/processes/{proc['id']}/logs").json()
    assert any("key-length 4" in line["text"] for line in logs["lines"])  # project .env reached the app
    assert client.delete(f"/api/processes/{proc['id']}").status_code == 409
    stopped = client.post(f"/api/processes/{proc['id']}/stop").json()
    deadline = time.time() + 10
    while stopped["status"] not in ("stopped", "failed", "exited") and time.time() < deadline:
        time.sleep(0.2)
        stopped = client.get(f"/api/processes/{proc['id']}").json()
    assert stopped["status"] == "stopped"
    assert client.delete(f"/api/processes/{proc['id']}").status_code == 200


def test_token_auth(fake_repo, tmp_path):
    app = create_app(Settings(repo_root=fake_repo, token="s3cret", data_dir=tmp_path / "d"))
    with TestClient(app) as c:
        assert c.get("/api/projects").status_code == 401
        assert c.post("/api/auth", json={"token": "nope"}).status_code == 401
        assert c.post("/api/auth", json={"token": "s3cret"}).status_code == 200
        assert c.get("/api/projects").status_code == 200  # cookie set
        assert c.get("/").status_code == 200
