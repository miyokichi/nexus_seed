"""Local web viewer/editor for little_agent projects (data/projects.json).

Run with: python skills/project_manager/scripts/viewer.py [--workspace PATH] [--port N] [--open]

Part of the project_manager skill, not of the little_agent package: the skill
owns its viewer so the core runtime never carries a per-skill dependency.
The project_manager skill's open_project_viewer tool starts this module detached.
Humans get full CRUD here: create projects, add/edit/delete tasks, change status.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import webbrowser
from contextlib import contextmanager, suppress
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

APP_NAME = "little-agent-viewer"
STATUSES = ("pending", "running", "done", "failed", "skipped")
ASSIGNEES = ("ai", "human")
FINISHED_STATUSES = {"done", "skipped"}
MAX_BODY_BYTES = 64 * 1024
COMMENT_MAX_CHARS = 2000
DEFAULT_PORT = 8765
INBOX_TITLE = "Inbox"


def load_state(workspace: Path) -> dict[str, Any]:
    """State served to the browser: projects.json plus derived 'ready' flags."""
    _ensure_migrated(workspace)
    path = _projects_path(workspace)
    mtime = path.stat().st_mtime_ns if path.exists() else None
    data = _load(workspace)
    projects = []
    for project in data.get("projects", []):
        if not isinstance(project, dict):
            continue
        ready = _ready_ids(project)
        entry = dict(project)
        entry["tasks"] = [dict(task, ready=task.get("id") in ready) for task in project.get("tasks") or []]
        projects.append(entry)
    return {"app": APP_NAME, "version": 2, "mtime": mtime, "projects": projects}


def viewer_create_project(workspace: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    """Create an empty project from the browser. Returns (ok, project_id_or_error)."""
    title = str(payload.get("title") or "").strip()
    if not title:
        return False, "プロジェクト名は必須です。"
    timestamp = now()
    project = {
        "id": _new_id(),
        "title": title,
        "goal": str(payload.get("goal") or "").strip(),
        "status": "active",
        "inbox": False,
        "created_at": timestamp,
        "updated_at": timestamp,
        "tasks": [],
    }
    with _locked(workspace):
        data = _load(workspace)
        data["projects"].append(project)
        _save(workspace, data)
    return True, project["id"]


def viewer_delete_project(workspace: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        return False, "project_id は必須です。"
    with _locked(workspace):
        data = _load(workspace)
        remaining = [p for p in data["projects"] if p.get("id") != project_id]
        if len(remaining) == len(data["projects"]):
            return False, f"プロジェクトが見つかりません: {project_id}"
        data["projects"] = remaining
        _save(workspace, data)
    return True, "ok"


def viewer_create_task(workspace: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    """Add a task from the browser. Returns (ok, task_id_or_error)."""
    project_id = str(payload.get("project_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not project_id:
        return False, "project_id は必須です。"
    if not title:
        return False, "タスク名は必須です。"
    assignee = str(payload.get("assignee") or "human").strip()
    if assignee not in ASSIGNEES:
        return False, f"担当が不正です: {assignee}"
    raw_depends = payload.get("depends_on") or []
    if not isinstance(raw_depends, list):
        return False, "depends_on はタスクIDの配列で指定してください。"
    depends_on = [str(dep).strip() for dep in raw_depends if str(dep).strip()]

    with _locked(workspace):
        data = _load(workspace)
        project = _project_by_id(data, project_id)
        if project is None:
            return False, f"プロジェクトが見つかりません: {project_id}"
        known = {task.get("id") for task in project.get("tasks") or []}
        unknown = [dep for dep in depends_on if dep not in known]
        if unknown:
            return False, f"存在しない依存タスクID: {', '.join(unknown)}"
        task = {
            "id": _new_id(),
            "title": title,
            "description": str(payload.get("description") or "").strip(),
            "assignee": assignee,
            "assignee_name": str(payload.get("assignee_name") or "").strip() if assignee == "human" else "",
            "status": "pending",
            "depends_on": depends_on,
            "due": str(payload.get("due") or "").strip(),
            "priority": str(payload.get("priority") or "").strip(),
            "result": "",
            "comments": [],
            "created_at": now(),
            "created_via": "viewer",
            "started_at": None,
            "completed_at": None,
            "completed_via": None,
        }
        project["tasks"].append(task)
        _refresh_project_status(project)
        _save(workspace, data)
    return True, task["id"]


def viewer_update_task(workspace: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    """Edit task fields and/or status from the browser."""
    project_id = str(payload.get("project_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if not project_id or not task_id:
        return False, "project_id と task_id は必須です。"

    with _locked(workspace):
        data = _load(workspace)
        project = _project_by_id(data, project_id)
        if project is None:
            return False, f"プロジェクトが見つかりません: {project_id}"
        task = next((t for t in project.get("tasks") or [] if t.get("id") == task_id), None)
        if task is None:
            return False, f"タスクが見つかりません: {task_id}"

        if "assignee" in payload:
            assignee = str(payload.get("assignee") or "").strip()
            if assignee not in ASSIGNEES:
                return False, f"担当が不正です: {assignee}"
            task["assignee"] = assignee
            if assignee == "ai":
                task["assignee_name"] = ""  # a person's name only applies to human tasks
        if "assignee_name" in payload:
            task["assignee_name"] = str(payload.get("assignee_name") or "").strip()
        for field in ("title", "description", "due", "priority", "result"):
            if field in payload:
                value = str(payload.get(field) or "").strip()
                if field == "title" and not value:
                    return False, "タスク名は空にできません。"
                task[field] = value
        if "depends_on" in payload:
            raw_depends = payload.get("depends_on") or []
            if not isinstance(raw_depends, list):
                return False, "depends_on はタスクIDの配列で指定してください。"
            depends_on = [str(dep).strip() for dep in raw_depends if str(dep).strip()]
            error = _validate_depends_edit(project, task_id, depends_on)
            if error:
                return False, error
            task["depends_on"] = depends_on
        if "status" in payload:
            status = str(payload.get("status") or "").strip()
            if status not in STATUSES:
                return False, f"状態が不正です: {status}"
            _apply_status(task, status, "viewer")

        _refresh_project_status(project)
        _save(workspace, data)
    return True, "ok"


def viewer_add_comment(workspace: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    """Append a progress comment to a task from the browser."""
    project_id = str(payload.get("project_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if not project_id or not task_id:
        return False, "project_id と task_id は必須です。"
    text = str(payload.get("text") or "").strip()
    if not text:
        return False, "コメントを入力してください。"
    text = text[:COMMENT_MAX_CHARS]
    with _locked(workspace):
        data = _load(workspace)
        project = _project_by_id(data, project_id)
        if project is None:
            return False, f"プロジェクトが見つかりません: {project_id}"
        task = next((t for t in project.get("tasks") or [] if t.get("id") == task_id), None)
        if task is None:
            return False, f"タスクが見つかりません: {task_id}"
        task.setdefault("comments", []).append({"at": now(), "via": "viewer", "text": text})
        _refresh_project_status(project)
        _save(workspace, data)
    return True, "ok"


def viewer_delete_task(workspace: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    project_id = str(payload.get("project_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if not project_id or not task_id:
        return False, "project_id と task_id は必須です。"
    with _locked(workspace):
        data = _load(workspace)
        project = _project_by_id(data, project_id)
        if project is None:
            return False, f"プロジェクトが見つかりません: {project_id}"
        tasks = project.get("tasks") or []
        remaining = [t for t in tasks if t.get("id") != task_id]
        if len(remaining) == len(tasks):
            return False, f"タスクが見つかりません: {task_id}"
        for other in remaining:
            deps = other.get("depends_on") or []
            if task_id in deps:
                other["depends_on"] = [dep for dep in deps if dep != task_id]
        project["tasks"] = remaining
        _refresh_project_status(project)
        _save(workspace, data)
    return True, "ok"


_POST_ROUTES = {
    "/api/project/create": viewer_create_project,
    "/api/project/delete": viewer_delete_project,
    "/api/task/create": viewer_create_task,
    "/api/task/update": viewer_update_task,
    "/api/task/comment": viewer_add_comment,
    "/api/task/delete": viewer_delete_task,
}


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "LittleAgentViewer/2.0"
    protocol_version = "HTTP/1.1"

    @property
    def workspace(self) -> Path:
        return self.server.workspace  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature.
        pass

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._send_json(403, {"ok": False, "error": "forbidden host"})
            return
        route = self.path.split("?", 1)[0].split("#", 1)[0]
        if route == "/":
            self._send_bytes(200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
        elif route == "/api/state":
            try:
                self._send_json(200, load_state(self.workspace))
            except Exception as exc:  # noqa: BLE001 - reported to the browser.
                self._send_json(500, {"ok": False, "error": str(exc)})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self._host_allowed():
            self._send_json(403, {"ok": False, "error": "forbidden host"})
            return
        route = self.path.split("?", 1)[0].split("#", 1)[0]
        handler = _POST_ROUTES.get(route)
        if handler is None:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"ok": False, "error": "invalid body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "body must be a JSON object"})
            return
        try:
            ok, message = handler(self.workspace, payload)
        except Exception as exc:  # noqa: BLE001 - reported to the browser.
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if ok:
            self._send_json(200, {"ok": True, "id": message})
        else:
            self._send_json(409, {"ok": False, "error": message})

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host.startswith("127.0.0.1") or host.startswith("localhost")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass


def make_server(workspace: Path, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)
    server.workspace = workspace.resolve()  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="viewer.py", description="Project viewer for little_agent.")
    parser.add_argument("--workspace", default=os.getenv("LITTLE_AGENT_WORKSPACE", "."), help="Workspace directory.")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("LITTLE_AGENT_VIEWER_PORT", str(DEFAULT_PORT))),
        help="Port to listen on (127.0.0.1 only).",
    )
    parser.add_argument("--open", action="store_true", help="Open the browser after start.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    _ensure_migrated(workspace)
    server = make_server(workspace, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"[viewer] workspace: {workspace}")
    print(f"[viewer] url: {url} (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# NOTE: keep the storage/migration helpers below in sync with
# skills/project_manager/scripts/project_tool.py. The duplication is intentional:
# skill scripts must not import little_agent so that skill folders stay copy-portable.
def _projects_path(workspace: Path) -> Path:
    path = (workspace / "data" / "projects.json").resolve()
    if workspace not in [path, *path.parents]:
        raise ValueError("Project path escaped the workspace.")
    return path


def _ensure_migrated(workspace: Path) -> bool:
    """One-time migration from legacy tasks.json / workflows.json to projects.json."""
    new_path = _projects_path(workspace)
    legacy_workflows = new_path.with_name("workflows.json")
    legacy_tasks = new_path.with_name("tasks.json")
    if new_path.exists() or (not legacy_workflows.exists() and not legacy_tasks.exists()):
        return False
    with _locked(workspace):
        if new_path.exists():  # another process migrated while we waited
            return False
        projects: list[dict[str, Any]] = []
        if legacy_workflows.exists():
            with suppress(json.JSONDecodeError, OSError):
                old = json.loads(legacy_workflows.read_text(encoding="utf-8"))
                for workflow in old.get("workflows") or []:
                    if isinstance(workflow, dict):
                        projects.append(_project_from_workflow(workflow))
        if legacy_tasks.exists():
            with suppress(json.JSONDecodeError, OSError):
                old_tasks = json.loads(legacy_tasks.read_text(encoding="utf-8"))
                if isinstance(old_tasks, list) and old_tasks:
                    projects.append(_inbox_from_legacy_tasks(old_tasks))
        _save(workspace, {"version": 2, "projects": projects})
        for legacy in (legacy_workflows, legacy_tasks):
            if legacy.exists():
                with suppress(OSError):
                    os.replace(legacy, legacy.with_name(legacy.name + ".bak"))
    return True


def _project_from_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    tasks = []
    for task in workflow.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        migrated = dict(task)
        migrated.setdefault("due", "")
        migrated.setdefault("priority", "")
        migrated.setdefault("assignee_name", "")
        migrated.setdefault("comments", [])
        migrated.setdefault("created_via", "agent")
        tasks.append(migrated)
    return {
        "id": str(workflow.get("id") or _new_id()),
        "title": str(workflow.get("title") or "(untitled)"),
        "goal": str(workflow.get("goal") or ""),
        "status": str(workflow.get("status") or "active"),
        "inbox": False,
        "created_at": str(workflow.get("created_at") or now()),
        "updated_at": str(workflow.get("updated_at") or now()),
        "tasks": tasks,
    }


def _inbox_from_legacy_tasks(old_tasks: list[Any]) -> dict[str, Any]:
    tasks = []
    for old in old_tasks:
        if not isinstance(old, dict):
            continue
        status = "done" if old.get("status") == "done" else "pending"
        tasks.append(
            {
                "id": str(old.get("id") or _new_id()),
                "title": str(old.get("title") or "(untitled)"),
                "description": str(old.get("notes") or ""),
                "assignee": "human",
                "assignee_name": "",
                "status": status,
                "depends_on": [],
                "due": str(old.get("due") or ""),
                "priority": str(old.get("priority") or ""),
                "result": "",
                "comments": [],
                "created_at": str(old.get("created_at") or now()),
                "created_via": "agent",
                "started_at": None,
                "completed_at": old.get("completed_at"),
                "completed_via": "agent" if status == "done" else None,
            }
        )
    timestamp = now()
    return {
        "id": _new_id(),
        "title": INBOX_TITLE,
        "goal": "task_manager から移行した単発タスク",
        "status": "active",
        "inbox": True,
        "created_at": timestamp,
        "updated_at": timestamp,
        "tasks": tasks,
    }


def _load(workspace: Path) -> dict[str, Any]:
    path = _projects_path(workspace)
    if not path.exists():
        return {"version": 2, "projects": []}
    data: Any = None
    for attempt in range(2):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            break
        except json.JSONDecodeError:
            if attempt == 1:
                raise
            time.sleep(0.05)
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise ValueError("data/projects.json must contain an object with a 'projects' list.")
    return data


def _save(workspace: Path, data: dict[str, Any]) -> None:
    path = _projects_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(3):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.1)


@contextmanager
def _locked(workspace: Path) -> Iterator[None]:
    """Cross-process mutation lock via an exclusively-created lock file."""
    path = _projects_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    for _attempt in range(40):
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(handle)
            break
        except FileExistsError:
            with suppress(OSError):
                if time.time() - lock_path.stat().st_mtime > 10:
                    lock_path.unlink()
                    continue
            time.sleep(0.05)
    else:
        raise TimeoutError("Could not acquire data/projects.json.lock within 2 seconds.")
    try:
        yield
    finally:
        with suppress(OSError):
            lock_path.unlink()


def _project_by_id(data: dict[str, Any], project_id: str) -> dict[str, Any] | None:
    return next(
        (p for p in data.get("projects", []) if isinstance(p, dict) and p.get("id") == project_id),
        None,
    )


def _validate_depends_edit(project: dict[str, Any], task_id: str, depends_on: list[str]) -> str | None:
    known = {task.get("id") for task in project.get("tasks") or []}
    unknown = [dep for dep in depends_on if dep not in known]
    if unknown:
        return f"存在しない依存タスクID: {', '.join(unknown)}"
    if task_id in depends_on:
        return "タスクは自分自身に依存できません。"
    nodes = [str(task.get("id")) for task in project.get("tasks") or []]
    edges = []
    for task in project.get("tasks") or []:
        deps = depends_on if task.get("id") == task_id else (task.get("depends_on") or [])
        edges.extend((str(dep), str(task.get("id"))) for dep in deps)
    cycle = _detect_cycle(nodes, edges)
    if cycle:
        return f"依存関係が循環しています: {', '.join(cycle)}"
    return None


def _detect_cycle(nodes: list[str], edges: list[tuple[str, str]]) -> list[str] | None:
    """Kahn's algorithm. Returns the node keys stuck in a cycle, or None."""
    indegree = {node: 0 for node in nodes}
    downstream: dict[str, list[str]] = {node: [] for node in nodes}
    for prerequisite, dependent in edges:
        indegree[dependent] += 1
        downstream[prerequisite].append(dependent)
    queue = [node for node in nodes if indegree[node] == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for nxt in downstream[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen == len(nodes):
        return None
    return [node for node in nodes if indegree[node] > 0]


def _apply_status(task: dict[str, Any], status: str, via: str) -> None:
    task["status"] = status
    if status == "running" and not task.get("started_at"):
        task["started_at"] = now()
    if status in ("pending", "running"):
        task["completed_at"] = None
        task["completed_via"] = None
    if status in ("done", "failed", "skipped"):
        task["completed_at"] = now()
        task["completed_via"] = via


def _ready_ids(project: dict[str, Any]) -> set[str]:
    tasks = project.get("tasks") or []
    finished = {task["id"] for task in tasks if task.get("status") in FINISHED_STATUSES}
    return {
        task["id"]
        for task in tasks
        if task.get("status") == "pending" and all(dep in finished for dep in task.get("depends_on") or [])
    }


def _refresh_project_status(project: dict[str, Any]) -> None:
    tasks = project.get("tasks") or []
    if project.get("inbox"):
        # The inbox is a permanent bucket; it never completes.
        project["status"] = "active"
    else:
        all_finished = bool(tasks) and all(task.get("status") in FINISHED_STATUSES for task in tasks)
        project["status"] = "done" if all_finished else "active"
    project["updated_at"] = now()


def _new_id() -> str:
    return uuid4().hex[:8]


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


INDEX_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>little_agent Projects</title>
<style>
:root { --bg:#f8fafc; --card:#ffffff; --line:#e2e8f0; --text:#0f172a; --muted:#64748b; }
* { box-sizing:border-box; }
body { margin:0; font-family:"Segoe UI","Yu Gothic UI",system-ui,sans-serif; background:var(--bg); color:var(--text); }
header { display:flex; align-items:center; gap:10px; padding:10px 16px; background:var(--card);
         border-bottom:1px solid var(--line); position:sticky; top:0; flex-wrap:wrap; z-index:10; }
header h1 { font-size:16px; margin:0; }
select, input, textarea { padding:6px 8px; border:1px solid var(--line); border-radius:6px; font-size:14px; }
#proj-select { max-width:300px; }
#progress { color:var(--muted); font-size:13px; }
#conn { width:10px; height:10px; border-radius:50%; background:#22c55e; margin-left:auto; }
#conn.bad { background:#ef4444; }
button { border:none; border-radius:6px; padding:6px 12px; font-size:13px; cursor:pointer; }
button.primary { background:#2563eb; color:#fff; }
button.primary:hover { background:#1d4ed8; }
button.ghost { background:#f1f5f9; color:#334155; border:1px solid var(--line); }
button.danger { background:#fee2e2; color:#991b1b; border:1px solid #fca5a5; }
button.done { background:#f59e0b; color:#fff; }
button.done:hover { background:#d97706; }
main { display:grid; grid-template-columns:1fr 320px; gap:12px; padding:12px 16px; align-items:start; }
@media (max-width:900px) { main { grid-template-columns:1fr; } }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }
.card h2 { font-size:13px; margin:0 0 8px; color:var(--muted); font-weight:600; }
#diagram { overflow:auto; min-height:180px; }
#diagram svg { max-width:100%; height:auto; }
#diagram pre { font-size:12px; overflow:auto; }
.banner { background:#fef3c7; border:1px solid #f59e0b; color:#78350f; padding:6px 10px;
          border-radius:6px; font-size:12px; margin-bottom:8px; }
ul.flat { list-style:none; margin:0; padding:0; }
ul.flat li { padding:6px 8px; border-bottom:1px solid var(--line); font-size:13px; }
aside { display:flex; flex-direction:column; gap:12px; }
.waiting-item { border:1px solid #f59e0b; background:#fffbeb; border-radius:8px; padding:8px 10px; margin-bottom:8px; }
.waiting-item .t { font-size:14px; font-weight:600; }
.waiting-item .d { font-size:12px; color:var(--muted); margin:4px 0; white-space:pre-wrap; }
#detail { font-size:13px; }
#detail dt { color:var(--muted); margin-top:8px; font-size:12px; }
#detail dd { margin:2px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; }
.comments-title { color:var(--muted); font-size:12px; font-weight:600; margin:12px 0 4px; }
.comments-list { max-height:180px; overflow:auto; display:flex; flex-direction:column; gap:6px; }
.comment { background:#f8fafc; border:1px solid var(--line); border-radius:6px; padding:6px 8px; }
.comment .meta { font-size:11px; color:var(--muted); margin-bottom:2px; }
.comment .body { font-size:13px; white-space:pre-wrap; overflow-wrap:anywhere; }
.comment-form { display:flex; flex-direction:column; gap:6px; margin-top:8px; }
.comment-form textarea { min-height:48px; resize:vertical; width:100%; }
.comment-form button { align-self:flex-end; }
#table-card { margin:0 16px 16px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; font-size:12px; }
tbody tr { cursor:pointer; }
tbody tr:hover { background:#f1f5f9; }
tbody tr.sel { background:#e0f2fe; }
.chip { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; border:1px solid transparent; }
.chip.pending { background:#f1f5f9; color:#334155; border-color:#94a3b8; }
.chip.running { background:#dbeafe; color:#1e3a8a; border-color:#3b82f6; }
.chip.done { background:#dcfce7; color:#14532d; border-color:#22c55e; }
.chip.failed { background:#fee2e2; color:#7f1d1d; border-color:#ef4444; }
.chip.skipped { background:#e2e8f0; color:#64748b; border-color:#94a3b8; }
.chip.ready { background:#fef3c7; color:#78350f; border-color:#f59e0b; font-weight:600; }
dialog { border:1px solid var(--line); border-radius:10px; padding:16px; max-width:480px; width:92vw; }
dialog::backdrop { background:rgba(15,23,42,0.35); }
dialog h3 { margin:0 0 12px; font-size:15px; }
dialog form { display:flex; flex-direction:column; gap:8px; }
dialog label { font-size:12px; color:var(--muted); display:flex; flex-direction:column; gap:3px; }
dialog input, dialog textarea, dialog select { width:100%; }
dialog textarea { min-height:60px; resize:vertical; }
.deps-box { max-height:130px; overflow:auto; border:1px solid var(--line); border-radius:6px; padding:6px; }
.deps-box label { flex-direction:row; align-items:center; gap:6px; font-size:13px; color:var(--text); }
.dlg-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:6px; }
.dlg-actions .danger { margin-right:auto; }
#toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#0f172a; color:#fff;
         padding:8px 14px; border-radius:8px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none; z-index:50; }
#toast.show { opacity:0.95; }
#empty { padding:32px; color:var(--muted); text-align:center; }
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js" onerror="window.__cdnFailed=true"></script>
</head>
<body>
<header>
  <h1>🗂️ little_agent Projects</h1>
  <select id="proj-select" title="プロジェクト選択"></select>
  <span id="progress"></span>
  <button class="primary" id="btn-new-task">＋タスク</button>
  <button class="ghost" id="btn-new-proj">＋プロジェクト</button>
  <button class="danger" id="btn-del-proj">プロジェクト削除</button>
  <span id="conn" title="接続状態"></span>
</header>
<div id="empty" hidden>プロジェクトはまだありません。「＋プロジェクト」から作るか、エージェントに「〜のプロジェクトを作って」と頼んでください。</div>
<main id="main">
  <section class="card" id="diagram-card">
    <h2>タスク図(🤖 AI / 👤 人間)</h2>
    <div id="diagram"></div>
  </section>
  <aside>
    <section class="card">
      <h2>あなた待ちのタスク</h2>
      <div id="waiting"></div>
    </section>
    <section class="card">
      <h2>タスク詳細</h2>
      <div id="detail">タスクをクリックすると詳細を表示します。</div>
    </section>
  </aside>
</main>
<section class="card" id="table-card">
  <h2>全タスク</h2>
  <table>
    <thead><tr><th>ID</th><th>担当</th><th>状態</th><th>タイトル</th><th>期限</th><th>優先度</th><th>依存</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
</section>

<dialog id="task-dlg">
  <h3 id="task-dlg-title">タスクを追加</h3>
  <form method="dialog" id="task-form">
    <label>タイトル<input id="f-title" required></label>
    <label>説明<textarea id="f-desc"></textarea></label>
    <label>担当
      <select id="f-assignee">
        <option value="human">👤 人間</option>
        <option value="ai">🤖 AI</option>
      </select>
    </label>
    <label id="f-assignee-name-row">担当者名(任意)<input id="f-assignee-name" placeholder="例: 山田さん"></label>
    <label id="f-status-row">状態
      <select id="f-status">
        <option value="pending">pending</option>
        <option value="running">running</option>
        <option value="done">done</option>
        <option value="failed">failed</option>
        <option value="skipped">skipped</option>
      </select>
    </label>
    <label>期限<input id="f-due" placeholder="例: 2026-07-15 / 今週中"></label>
    <label>優先度
      <select id="f-priority">
        <option value="">(なし)</option>
        <option value="low">low</option>
        <option value="normal">normal</option>
        <option value="high">high</option>
      </select>
    </label>
    <label>依存タスク(先に完了が必要)</label>
    <div class="deps-box" id="f-deps"></div>
    <div class="dlg-actions">
      <button type="button" class="danger" id="btn-del-task" hidden>削除</button>
      <button type="button" class="ghost" id="btn-cancel-task">キャンセル</button>
      <button type="submit" class="primary" id="btn-save-task">保存</button>
    </div>
  </form>
</dialog>

<dialog id="proj-dlg">
  <h3>プロジェクトを作成</h3>
  <form method="dialog" id="proj-form">
    <label>プロジェクト名<input id="p-title" required></label>
    <label>ゴール(任意)<textarea id="p-goal"></textarea></label>
    <div class="dlg-actions">
      <button type="button" class="ghost" id="btn-cancel-proj">キャンセル</button>
      <button type="submit" class="primary">作成</button>
    </div>
  </form>
</dialog>

<div id="toast"></div>
<script>
const POLL_MS = 1500;
let lastMtime;
let state = null;
let currentProjId = (location.hash.match(/p=([0-9a-fA-F]+)/) || [])[1] || null;
let selectedTaskId = null;
let editingTaskId = null;   // null = dialog adds a new task
let optionsKey = '';
let renderSeq = 0;
let toastTimer;
const $ = (id) => document.getElementById(id);

if (window.mermaid) {
  window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral' });
}

function connOk(ok) { $('conn').classList.toggle('bad', !ok); }
function isFinished(t) { return t.status === 'done' || t.status === 'skipped'; }
function assigneeLabel(t) {
  if (t.assignee === 'human') return '👤 ' + ((t.assignee_name || '').trim() || '人間');
  return '🤖 AI';
}

async function post(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || '操作に失敗しました');
  return data;
}

async function tick(force) {
  let data;
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    if (!res.ok) throw new Error('bad status ' + res.status);
    data = await res.json();
  } catch (err) {
    connOk(false);
    return;
  }
  connOk(true);
  const changed = force || !state || data.mtime !== lastMtime;
  lastMtime = data.mtime;
  if (!changed) return;
  state = data;
  render();
}

function currentProj() {
  const ps = (state && state.projects) || [];
  return ps.find(p => p.id === currentProjId)
    || ps.find(p => p.status === 'active' && !p.inbox)
    || ps.find(p => p.status === 'active')
    || ps[ps.length - 1]
    || null;
}

function render() {
  const ps = state.projects || [];
  const hasAny = ps.length > 0;
  $('empty').hidden = hasAny;
  $('main').style.display = hasAny ? '' : 'none';
  $('table-card').style.display = hasAny ? '' : 'none';
  $('btn-new-task').disabled = !hasAny;
  $('btn-del-proj').disabled = !hasAny;
  if (!hasAny) { $('progress').textContent = ''; return; }

  const proj = currentProj();
  currentProjId = proj.id;

  const key = JSON.stringify(ps.map(p => [p.id, p.title, p.status]));
  if (key !== optionsKey) {
    optionsKey = key;
    const sel = $('proj-select');
    sel.innerHTML = '';
    for (const p of ps) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = (p.status === 'done' ? '✅ ' : '') + (p.inbox ? '📥 ' : '') + p.title;
      sel.appendChild(opt);
    }
  }
  $('proj-select').value = proj.id;

  const doneCount = proj.tasks.filter(isFinished).length;
  $('progress').textContent = '完了 ' + doneCount + '/' + proj.tasks.length;

  renderWaiting(proj);
  renderTable(proj);
  renderDetail(proj);
  renderDiagram(proj);
}

function renderWaiting(proj) {
  const box = $('waiting');
  box.innerHTML = '';
  const ready = proj.tasks.filter(t => t.ready && t.assignee === 'human');
  if (!ready.length) {
    box.textContent = 'いまあなた待ちのタスクはありません。';
    return;
  }
  for (const t of ready) {
    const card = document.createElement('div');
    card.className = 'waiting-item';
    const title = document.createElement('div');
    title.className = 't';
    title.textContent = assigneeLabel(t) + ': ' + t.title;
    card.appendChild(title);
    if (t.description) {
      const desc = document.createElement('div');
      desc.className = 'd';
      desc.textContent = t.description;
      card.appendChild(desc);
    }
    const btn = document.createElement('button');
    btn.className = 'done';
    btn.textContent = '完了にする';
    btn.addEventListener('click', () => completeTask(proj.id, t.id));
    card.appendChild(btn);
    box.appendChild(card);
  }
}

async function completeTask(projId, taskId) {
  try {
    await post('/api/task/update', { project_id: projId, task_id: taskId, status: 'done' });
    toast('完了にしました');
  } catch (err) {
    toast(err.message);
  }
  tick(true);
}

function statusChip(t) {
  const isWait = t.ready && t.assignee === 'human';
  const span = document.createElement('span');
  span.className = 'chip ' + (isWait ? 'ready' : t.status);
  span.textContent = isWait ? 'あなた待ち' : t.status;
  return span;
}

function renderTable(proj) {
  const tbody = $('rows');
  tbody.innerHTML = '';
  const byId = {};
  for (const t of proj.tasks) byId[t.id] = t;
  for (const t of proj.tasks) {
    const tr = document.createElement('tr');
    tr.dataset.id = t.id;
    if (t.id === selectedTaskId) tr.className = 'sel';
    const cells = [
      t.id,
      assigneeLabel(t),
      null,
      t.title,
      t.due || '-',
      t.priority || '-',
      (t.depends_on || []).map(d => (byId[d] || { title: d }).title).join(', ') || '-'
    ];
    cells.forEach((value, i) => {
      const td = document.createElement('td');
      if (i === 2) td.appendChild(statusChip(t));
      else td.textContent = value;
      tr.appendChild(td);
    });
    tr.addEventListener('click', () => selectTask(t.id));
    tr.addEventListener('dblclick', () => openTaskDialog(t.id));
    tbody.appendChild(tr);
  }
}

function selectTask(id) {
  selectedTaskId = id;
  document.querySelectorAll('#rows tr').forEach(tr => {
    tr.classList.toggle('sel', tr.dataset.id === id);
  });
  renderDetail(currentProj());
}

function renderDetail(proj) {
  const box = $('detail');
  const t = proj && proj.tasks.find(x => x.id === selectedTaskId);
  if (!t) {
    box.textContent = 'タスクをクリックすると詳細を表示します。ダブルクリックで編集できます。';
    return;
  }
  const byId = {};
  for (const x of proj.tasks) byId[x.id] = x;
  // Preserve a comment draft across the 1.5s poll re-render.
  const prevInput = document.getElementById('comment-input');
  const draft = (prevInput && prevInput.dataset.taskId === t.id) ? prevInput.value : '';
  box.innerHTML = '';
  const dl = document.createElement('dl');
  dl.style.margin = '0';
  const add = (label, value) => {
    if (!value) return;
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = value;
    dl.appendChild(dt);
    dl.appendChild(dd);
  };
  add('タイトル', t.title);
  add('担当 / 状態', assigneeLabel(t) + ' / ' + t.status + (t.ready ? ' (着手可能)' : ''));
  add('説明', t.description);
  add('期限', t.due);
  add('優先度', t.priority);
  add('依存', (t.depends_on || []).map(d => (byId[d] || { title: d }).title).join(', '));
  add('結果', t.result);
  add('作成', t.created_at + (t.created_via === 'viewer' ? ' (ブラウザから)' : ''));
  add('開始', t.started_at);
  add('完了', t.completed_at
    ? t.completed_at + (t.completed_via ? (t.completed_via === 'viewer' ? ' (ブラウザから)' : ' (エージェント)') : '')
    : '');
  box.appendChild(dl);
  const btn = document.createElement('button');
  btn.className = 'ghost';
  btn.textContent = '✏️ 編集';
  btn.style.marginTop = '10px';
  btn.addEventListener('click', () => openTaskDialog(t.id));
  box.appendChild(btn);
  renderComments(box, proj, t, draft);
}

function renderComments(box, proj, t, draft) {
  const title = document.createElement('div');
  title.className = 'comments-title';
  const comments = t.comments || [];
  title.textContent = '進捗コメント (' + comments.length + ')';
  box.appendChild(title);
  const list = document.createElement('div');
  list.className = 'comments-list';
  if (!comments.length) {
    list.textContent = 'まだコメントはありません。';
    list.style.color = 'var(--muted)';
    list.style.fontSize = '12px';
  }
  for (const c of comments) {
    const item = document.createElement('div');
    item.className = 'comment';
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = (c.via === 'viewer' ? '👤 あなた' : '🤖 エージェント') + ' ・ ' + (c.at || '');
    const body = document.createElement('div');
    body.className = 'body';
    body.textContent = c.text || '';
    item.appendChild(meta);
    item.appendChild(body);
    list.appendChild(item);
  }
  box.appendChild(list);
  if (comments.length) list.scrollTop = list.scrollHeight;

  const form = document.createElement('div');
  form.className = 'comment-form';
  const input = document.createElement('textarea');
  input.id = 'comment-input';
  input.dataset.taskId = t.id;
  input.placeholder = '進捗コメントを入力…';
  input.value = draft || '';
  const send = document.createElement('button');
  send.className = 'primary';
  send.textContent = 'コメントを追加';
  send.addEventListener('click', async () => {
    const text = input.value.trim();
    if (!text) return;
    try {
      await post('/api/task/comment', { project_id: proj.id, task_id: t.id, text: text });
      input.value = '';
      toast('コメントを追加しました');
    } catch (err) {
      toast(err.message);
    }
    tick(true);
  });
  form.appendChild(input);
  form.appendChild(send);
  box.appendChild(form);
}

function openTaskDialog(taskId) {
  const proj = currentProj();
  if (!proj) return;
  editingTaskId = taskId || null;
  const t = taskId ? proj.tasks.find(x => x.id === taskId) : null;
  $('task-dlg-title').textContent = t ? 'タスクを編集' : 'タスクを追加';
  $('f-title').value = t ? t.title : '';
  $('f-desc').value = t ? (t.description || '') : '';
  $('f-assignee').value = t ? t.assignee : 'human';
  $('f-assignee-name').value = t ? (t.assignee_name || '') : '';
  $('f-status-row').hidden = !t;
  $('f-status').value = t ? t.status : 'pending';
  $('f-due').value = t ? (t.due || '') : '';
  $('f-priority').value = t ? (t.priority || '') : '';
  $('btn-del-task').hidden = !t;
  syncAssigneeNameRow();
  const deps = new Set(t ? (t.depends_on || []) : []);
  const box = $('f-deps');
  box.innerHTML = '';
  const others = proj.tasks.filter(x => !taskId || x.id !== taskId);
  if (!others.length) box.textContent = '(依存できるタスクはありません)';
  for (const other of others) {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = other.id;
    cb.checked = deps.has(other.id);
    label.appendChild(cb);
    label.appendChild(document.createTextNode(assigneeLabel(other) + ' ' + other.title));
    box.appendChild(label);
  }
  $('task-dlg').showModal();
}

function syncAssigneeNameRow() {
  // Only human tasks can carry a person's name.
  $('f-assignee-name-row').hidden = $('f-assignee').value !== 'human';
}

async function saveTaskDialog(event) {
  event.preventDefault();
  const proj = currentProj();
  if (!proj) return;
  const isHuman = $('f-assignee').value === 'human';
  const body = {
    project_id: proj.id,
    title: $('f-title').value.trim(),
    description: $('f-desc').value.trim(),
    assignee: $('f-assignee').value,
    assignee_name: isHuman ? $('f-assignee-name').value.trim() : '',
    due: $('f-due').value.trim(),
    priority: $('f-priority').value,
    depends_on: Array.from($('f-deps').querySelectorAll('input:checked')).map(cb => cb.value)
  };
  try {
    if (editingTaskId) {
      body.task_id = editingTaskId;
      body.status = $('f-status').value;
      await post('/api/task/update', body);
      toast('保存しました');
    } else {
      const data = await post('/api/task/create', body);
      selectedTaskId = data.id;
      toast('タスクを追加しました');
    }
    $('task-dlg').close();
  } catch (err) {
    toast(err.message);
  }
  tick(true);
}

async function deleteTaskFromDialog() {
  const proj = currentProj();
  if (!proj || !editingTaskId) return;
  if (!confirm('このタスクを削除しますか？元に戻せません。')) return;
  try {
    await post('/api/task/delete', { project_id: proj.id, task_id: editingTaskId });
    if (selectedTaskId === editingTaskId) selectedTaskId = null;
    toast('削除しました');
    $('task-dlg').close();
  } catch (err) {
    toast(err.message);
  }
  tick(true);
}

async function saveProjectDialog(event) {
  event.preventDefault();
  try {
    const data = await post('/api/project/create', {
      title: $('p-title').value.trim(),
      goal: $('p-goal').value.trim()
    });
    currentProjId = data.id;
    selectedTaskId = null;
    history.replaceState(null, '', '#p=' + currentProjId);
    toast('プロジェクトを作成しました');
    $('proj-dlg').close();
  } catch (err) {
    toast(err.message);
  }
  tick(true);
}

async function deleteCurrentProject() {
  const proj = currentProj();
  if (!proj) return;
  if (!confirm('プロジェクト「' + proj.title + '」と全タスクを削除しますか？元に戻せません。')) return;
  try {
    await post('/api/project/delete', { project_id: proj.id });
    currentProjId = null;
    selectedTaskId = null;
    toast('プロジェクトを削除しました');
  } catch (err) {
    toast(err.message);
  }
  tick(true);
}

function mermaidLabel(text) {
  let s = String(text || '').replace(/#/g, '#35;').replace(/"/g, '#quot;').replace(/\r?\n/g, ' ');
  if (s.length > 40) s = s.slice(0, 39) + '…';
  return s;
}

function mermaidText(proj) {
  const lines = ['flowchart TD'];
  for (const t of proj.tasks) {
    const who = t.assignee === 'human' ? ((t.assignee_name || '').trim() ? '👤' + mermaidLabel(t.assignee_name) + ': ' : '👤 ') : '🤖 ';
    const label = '"' + who + mermaidLabel(t.title) + '"';
    const cls = (t.ready && t.assignee === 'human') ? 'ready' : t.status;
    const node = t.assignee === 'human'
      ? 't_' + t.id + '[/' + label + '/]'
      : 't_' + t.id + '[' + label + ']';
    lines.push('  ' + node + ':::' + cls);
  }
  for (const t of proj.tasks) {
    for (const d of t.depends_on || []) lines.push('  t_' + d + ' --> t_' + t.id);
  }
  lines.push('  classDef pending fill:#f1f5f9,stroke:#94a3b8,color:#334155;');
  lines.push('  classDef running fill:#dbeafe,stroke:#3b82f6,color:#1e3a8a,stroke-width:2px;');
  lines.push('  classDef done fill:#dcfce7,stroke:#22c55e,color:#14532d;');
  lines.push('  classDef failed fill:#fee2e2,stroke:#ef4444,color:#7f1d1d;');
  lines.push('  classDef skipped fill:#e2e8f0,stroke:#94a3b8,color:#64748b,stroke-dasharray:4 3;');
  lines.push('  classDef ready fill:#fef3c7,stroke:#f59e0b,color:#78350f,stroke-width:3px;');
  return lines.join('\n');
}

async function renderDiagram(proj) {
  const container = $('diagram');
  if (!proj.tasks.length) {
    container.innerHTML = '<div class="banner">タスクがありません。「＋タスク」から追加してください。</div>';
    container.dataset.src = '';
    return;
  }
  if (!window.mermaid) {
    renderFallback(proj, container);
    return;
  }
  const text = mermaidText(proj);
  if (container.dataset.src === text) return;
  try {
    const result = await window.mermaid.render('projgraph' + (renderSeq++), text);
    container.innerHTML = result.svg;
  } catch (err) {
    container.innerHTML = '';
    const pre = document.createElement('pre');
    pre.textContent = text;
    container.appendChild(pre);
  }
  container.dataset.src = text;
}

function renderFallback(proj, container) {
  container.innerHTML = '';
  const banner = document.createElement('div');
  banner.className = 'banner';
  banner.textContent = 'オフライン表示: Mermaid (CDN) を読み込めないため、図の代わりに一覧を表示しています。';
  container.appendChild(banner);
  const byId = {};
  for (const t of proj.tasks) byId[t.id] = t;
  const ul = document.createElement('ul');
  ul.className = 'flat';
  for (const t of proj.tasks) {
    const li = document.createElement('li');
    const deps = (t.depends_on || []).map(d => (byId[d] || { title: d }).title).join(', ');
    li.textContent = '[' + t.status + '] ' + assigneeLabel(t) + ' ' + t.title
      + (deps ? ' ← 依存: ' + deps : '');
    ul.appendChild(li);
  }
  container.appendChild(ul);
}

function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}

$('proj-select').addEventListener('change', (event) => {
  currentProjId = event.target.value;
  selectedTaskId = null;
  history.replaceState(null, '', '#p=' + currentProjId);
  render();
});
$('btn-new-proj').addEventListener('click', () => { $('p-title').value = ''; $('p-goal').value = ''; $('proj-dlg').showModal(); });
$('btn-del-proj').addEventListener('click', deleteCurrentProject);
$('btn-new-task').addEventListener('click', () => openTaskDialog(null));
$('f-assignee').addEventListener('change', syncAssigneeNameRow);
$('task-form').addEventListener('submit', saveTaskDialog);
$('btn-cancel-task').addEventListener('click', () => $('task-dlg').close());
$('btn-del-task').addEventListener('click', deleteTaskFromDialog);
$('proj-form').addEventListener('submit', saveProjectDialog);
$('btn-cancel-proj').addEventListener('click', () => $('proj-dlg').close());

tick(true);
setInterval(() => tick(false), POLL_MS);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
