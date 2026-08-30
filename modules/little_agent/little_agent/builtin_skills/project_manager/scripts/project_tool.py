from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

STATUSES = ("pending", "running", "done", "failed", "skipped")
ASSIGNEES = ("ai", "human")
FINISHED_STATUSES = {"done", "skipped"}
RESULT_MAX_CHARS = 2000
COMMENT_MAX_CHARS = 2000
DEFAULT_VIEWER_PORT = 8765
INBOX_TITLE = "Inbox"
# The viewer ships inside this skill folder rather than in the little_agent
# package, so the skill carries its own dependency and the core runtime carries
# none of the skill's.
VIEWER_SCRIPT = Path(__file__).resolve().parent / "viewer.py"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        _ensure_migrated(workspace)

        if tool == "create_project":
            result = create_project(workspace, arguments)
        elif tool == "add_task":
            result = add_task(workspace, arguments)
        elif tool == "update_task":
            result = update_task(workspace, arguments)
        elif tool == "update_task_status":
            result = update_task_status(workspace, arguments)
        elif tool == "add_task_comment":
            result = add_task_comment(workspace, arguments)
        elif tool == "show_project":
            result = show_project(workspace, arguments)
        elif tool == "list_projects":
            result = list_projects(workspace, arguments)
        elif tool == "list_tasks":
            result = list_tasks(workspace, arguments)
        elif tool == "delete_task":
            result = delete_task(workspace, arguments)
        elif tool == "delete_project":
            result = delete_project(workspace, arguments)
        elif tool == "open_project_viewer":
            result = open_project_viewer(workspace, arguments)
        else:
            result = {"ok": False, "content": f"Unknown project tool: {tool}"}
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Project script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def create_project(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()
    if not title:
        return {"ok": False, "content": "Project title is required."}
    goal = str(arguments.get("goal") or "").strip()
    items = arguments.get("tasks") or []
    if not isinstance(items, list):
        return {"ok": False, "content": "tasks must be an array."}

    if items:
        error = _validate_new_tasks(items)
        if error:
            return {"ok": False, "content": error}
        keys = [str(item["key"]).strip() for item in items]
        edges = [
            (str(dep).strip(), str(item["key"]).strip())
            for item in items
            for dep in item.get("depends_on") or []
        ]
        cycle = _detect_cycle(keys, edges)
        if cycle:
            return {"ok": False, "content": f"Dependency cycle detected among tasks: {', '.join(cycle)}"}
        key_to_id = {key: _new_id() for key in keys}
        tasks = [
            _new_task(
                title=str(item["title"]).strip(),
                description=str(item.get("description") or "").strip(),
                assignee=str(item["assignee"]).strip(),
                depends_on=[key_to_id[str(dep).strip()] for dep in item.get("depends_on") or []],
                due=str(item.get("due") or "").strip(),
                priority=str(item.get("priority") or "").strip(),
                assignee_name=str(item.get("assignee_name") or "").strip(),
                task_id=key_to_id[str(item["key"]).strip()],
            )
            for item in items
        ]
    else:
        key_to_id = {}
        tasks = []

    timestamp = now()
    project = {
        "id": _new_id(),
        "title": title,
        "goal": goal,
        "status": "active",
        "inbox": False,
        "created_at": timestamp,
        "updated_at": timestamp,
        "tasks": tasks,
    }
    with _locked(workspace):
        data = _load(workspace)
        data["projects"].append(project)
        _save(workspace, data)
    mapping = ", ".join(f"{key}={key_to_id[key]}" for key in key_to_id)
    header = f"Created project {project['id']}."
    if mapping:
        header += f"\nTask ids: {mapping}"
    return {"ok": True, "content": f"{header}\n{_format_project(project)}"}


def add_task(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()
    if not title:
        return {"ok": False, "content": "Task title is required."}
    assignee = str(arguments.get("assignee") or "human").strip()
    if assignee not in ASSIGNEES:
        return {"ok": False, "content": f"Invalid assignee '{assignee}'. Use one of: ai, human."}
    raw_depends = arguments.get("depends_on") or []
    if not isinstance(raw_depends, list):
        return {"ok": False, "content": "depends_on must be an array of task ids."}
    depends_on = [str(dep).strip() for dep in raw_depends if str(dep).strip()]

    with _locked(workspace):
        data = _load(workspace)
        wanted = str(arguments.get("project_id") or "").strip()
        if wanted:
            project, error = _find_project(data, wanted)
            if project is None:
                return {"ok": False, "content": error}
        else:
            project = _inbox_project(data)
        known = {task.get("id") for task in project["tasks"]}
        unknown = [dep for dep in depends_on if dep not in known]
        if unknown:
            return {
                "ok": False,
                "content": f"Unknown depends_on task ids: {', '.join(unknown)}. Check ids with show_project.",
            }
        task = _new_task(
            title=title,
            description=str(arguments.get("description") or "").strip(),
            assignee=assignee,
            depends_on=depends_on,
            due=str(arguments.get("due") or "").strip(),
            priority=str(arguments.get("priority") or "").strip(),
            assignee_name=str(arguments.get("assignee_name") or "").strip(),
        )
        project["tasks"].append(task)
        _refresh_project_status(project)
        _save(workspace, data)
        content = f"Added task to project {project['id']} ({project['title']}):\n{_format_task(task, _ready_ids(project))}"
    return {"ok": True, "content": content}


def update_task(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id:
        return {"ok": False, "content": "task_id is required."}

    with _locked(workspace):
        data = _load(workspace)
        project, task, error = _locate_task(data, arguments.get("project_id"), task_id)
        if project is None or task is None:
            return {"ok": False, "content": error}

        if "assignee" in arguments:
            assignee = str(arguments.get("assignee") or "").strip()
            if assignee not in ASSIGNEES:
                return {"ok": False, "content": f"Invalid assignee '{assignee}'. Use one of: ai, human."}
            task["assignee"] = assignee
            if assignee == "ai":
                task["assignee_name"] = ""  # a person's name only applies to human tasks
        if "assignee_name" in arguments:
            task["assignee_name"] = str(arguments.get("assignee_name") or "").strip()
        for field in ("title", "description", "due", "priority"):
            if field in arguments:
                value = str(arguments.get(field) or "").strip()
                if field == "title" and not value:
                    return {"ok": False, "content": "title must not be empty."}
                task[field] = value
        if "depends_on" in arguments:
            raw_depends = arguments.get("depends_on") or []
            if not isinstance(raw_depends, list):
                return {"ok": False, "content": "depends_on must be an array of task ids."}
            depends_on = [str(dep).strip() for dep in raw_depends if str(dep).strip()]
            error = _validate_depends_edit(project, task_id, depends_on)
            if error:
                return {"ok": False, "content": error}
            task["depends_on"] = depends_on

        _refresh_project_status(project)
        _save(workspace, data)
        content = _format_task(task, _ready_ids(project))
    return {"ok": True, "content": content}


def update_task_status(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id:
        return {"ok": False, "content": "task_id is required."}
    status = str(arguments.get("status") or "").strip()
    if status not in STATUSES:
        return {"ok": False, "content": f"Invalid status '{status}'. Use one of: {', '.join(STATUSES)}."}
    result_text = str(arguments.get("result") or "").strip()[:RESULT_MAX_CHARS]

    with _locked(workspace):
        data = _load(workspace)
        project, task, error = _locate_task(data, arguments.get("project_id"), task_id)
        if project is None or task is None:
            return {"ok": False, "content": error}

        warnings: list[str] = []
        if status == "done":
            finished = {t["id"] for t in project["tasks"] if t.get("status") in FINISHED_STATUSES}
            unmet = [dep for dep in task.get("depends_on") or [] if dep not in finished]
            if unmet:
                warnings.append(f"Note: marked done while dependencies are unfinished: {', '.join(unmet)}")

        _apply_status(task, status, "agent")
        if result_text:
            task["result"] = result_text
        _refresh_project_status(project)
        _save(workspace, data)

        ready = _ready_ids(project)
        ready_line = ", ".join(f"{t['id']}({t.get('assignee')})" for t in project["tasks"] if t["id"] in ready) or "-"
        content = "\n".join([_format_task(task, ready), *warnings, f"READY: {ready_line}"])
    return {"ok": True, "content": content}


def add_task_comment(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id:
        return {"ok": False, "content": "task_id is required."}
    text = str(arguments.get("text") or "").strip()
    if not text:
        return {"ok": False, "content": "text is required."}
    text = text[:COMMENT_MAX_CHARS]

    with _locked(workspace):
        data = _load(workspace)
        project, task, error = _locate_task(data, arguments.get("project_id"), task_id)
        if project is None or task is None:
            return {"ok": False, "content": error}
        comment = {"at": now(), "via": "agent", "text": text}
        task.setdefault("comments", []).append(comment)
        _refresh_project_status(project)
        _save(workspace, data)
        count = len(task["comments"])
    return {"ok": True, "content": f"Added comment #{count} to task {task_id}: {text}"}


def show_project(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    data = _load(workspace)
    project, error = _find_project(data, arguments.get("project_id"))
    if project is None:
        return {"ok": False, "content": error}
    return {"ok": True, "content": _format_project(project)}


def list_projects(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    wanted = str(arguments.get("status") or "active")
    data = _load(workspace)
    projects = [p for p in data["projects"] if isinstance(p, dict)]
    if wanted != "all":
        projects = [p for p in projects if p.get("status") == wanted]
    if not projects:
        return {"ok": True, "content": "(no projects)"}
    lines = []
    for project in projects:
        tasks = project.get("tasks") or []
        done_count = sum(1 for task in tasks if task.get("status") in FINISHED_STATUSES)
        marker = " [inbox]" if project.get("inbox") else ""
        lines.append(
            f"{project.get('id')} [{project.get('status')}]{marker} {project.get('title')} "
            f"(done {done_count}/{len(tasks)}, ready {len(_ready_ids(project))})"
        )
    return {"ok": True, "content": "\n".join(lines)}


def list_tasks(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    wanted = str(arguments.get("status") or "open")
    project_filter = str(arguments.get("project_id") or "").strip()
    data = _load(workspace)
    lines: list[str] = []
    for project in data["projects"]:
        if not isinstance(project, dict):
            continue
        if project_filter and project.get("id") != project_filter:
            continue
        ready = _ready_ids(project)
        for task in project.get("tasks") or []:
            status = task.get("status", "pending")
            if wanted == "open" and status not in ("pending", "running"):
                continue
            if wanted == "done" and status not in FINISHED_STATUSES:
                continue
            lines.append(f"[{project.get('title')}] {_format_task(task, ready)}")
    if not lines:
        return {"ok": True, "content": "(no tasks)"}
    return {"ok": True, "content": "\n".join(lines)}


def delete_task(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id:
        return {"ok": False, "content": "task_id is required."}
    with _locked(workspace):
        data = _load(workspace)
        project, task, error = _locate_task(data, arguments.get("project_id"), task_id)
        if project is None or task is None:
            return {"ok": False, "content": error}
        project["tasks"] = [t for t in project["tasks"] if t.get("id") != task_id]
        # Drop dangling dependency references.
        for other in project["tasks"]:
            deps = other.get("depends_on") or []
            if task_id in deps:
                other["depends_on"] = [dep for dep in deps if dep != task_id]
        _refresh_project_status(project)
        _save(workspace, data)
    return {"ok": True, "content": f"Deleted task {task_id} from project {project['id']}."}


def delete_project(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = str(arguments.get("project_id") or "").strip()
    if not project_id:
        return {"ok": False, "content": "project_id is required."}
    with _locked(workspace):
        data = _load(workspace)
        remaining = [p for p in data["projects"] if p.get("id") != project_id]
        if len(remaining) == len(data["projects"]):
            return {"ok": False, "content": f"Project not found: {project_id}"}
        data["projects"] = remaining
        _save(workspace, data)
    return {"ok": True, "content": f"Deleted project: {project_id}"}


def open_project_viewer(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    raw_port = arguments.get("port") or os.environ.get("LITTLE_AGENT_VIEWER_PORT") or DEFAULT_VIEWER_PORT
    port = int(raw_port)
    url = f"http://127.0.0.1:{port}/"
    project_id = str(arguments.get("project_id") or "").strip()
    fragment = f"#p={project_id}" if project_id else ""

    state = _viewer_state(port)
    if state == "other":
        return {
            "ok": False,
            "content": f"Port {port} is used by another app. Set LITTLE_AGENT_VIEWER_PORT or pass another port.",
        }
    if state == "down":
        # DEVNULL is required: the parent ScriptSkillTool waits on this script with
        # capture_output=True, so an inherited pipe would block it until the viewer exits.
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, str(VIEWER_SCRIPT), "--workspace", str(workspace), "--port", str(port)],
            **popen_kwargs,
        )
        for _attempt in range(6):
            time.sleep(0.5)
            if _viewer_state(port) == "ours":
                break
        else:
            return {
                "ok": False,
                "content": (
                    f"Viewer did not start on port {port}. "
                    f"Try manually: python \"{VIEWER_SCRIPT}\" --workspace \"{workspace}\""
                ),
            }
    webbrowser.open(url + fragment)
    already = " (already running)" if state == "ours" else ""
    return {"ok": True, "content": f"Project viewer{already}: {url}{fragment} - opened in the browser."}


def _viewer_state(port: int) -> str:
    """Return 'ours' | 'other' | 'down' for what is listening on the port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        return "other"
    except OSError:
        return "down"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "other"
    if isinstance(payload, dict) and payload.get("app") == "little-agent-viewer":
        return "ours"
    return "other"


def _validate_new_tasks(items: list[Any]) -> str | None:
    keys: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return f"tasks[{index}] must be an object."
        key = str(item.get("key") or "").strip()
        if not key:
            return f"tasks[{index}] is missing 'key'."
        if key in keys:
            return f"Duplicate task key: {key}"
        keys.add(key)
        if not str(item.get("title") or "").strip():
            return f"Task '{key}' is missing 'title'."
        assignee = str(item.get("assignee") or "").strip()
        if assignee not in ASSIGNEES:
            return f"Task '{key}' has invalid assignee '{assignee}'. Use one of: ai, human."
        depends_on = item.get("depends_on") or []
        if not isinstance(depends_on, list):
            return f"Task '{key}' depends_on must be an array of task keys."
    for item in items:
        key = str(item.get("key")).strip()
        for dep in item.get("depends_on") or []:
            dep_key = str(dep).strip()
            if dep_key == key:
                return f"Task '{key}' depends on itself."
            if dep_key not in keys:
                return f"Task '{key}' depends on unknown key '{dep_key}'."
    return None


def _validate_depends_edit(project: dict[str, Any], task_id: str, depends_on: list[str]) -> str | None:
    known = {task.get("id") for task in project.get("tasks") or []}
    unknown = [dep for dep in depends_on if dep not in known]
    if unknown:
        return f"Unknown depends_on task ids: {', '.join(unknown)}."
    if task_id in depends_on:
        return "A task cannot depend on itself."
    nodes = [str(task.get("id")) for task in project.get("tasks") or []]
    edges = []
    for task in project.get("tasks") or []:
        deps = depends_on if task.get("id") == task_id else (task.get("depends_on") or [])
        edges.extend((str(dep), str(task.get("id"))) for dep in deps)
    cycle = _detect_cycle(nodes, edges)
    if cycle:
        return f"Dependency cycle detected among tasks: {', '.join(cycle)}"
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


def _new_task(
    title: str,
    description: str,
    assignee: str,
    depends_on: list[str],
    due: str = "",
    priority: str = "",
    assignee_name: str = "",
    task_id: str | None = None,
    created_via: str = "agent",
) -> dict[str, Any]:
    return {
        "id": task_id or _new_id(),
        "title": title,
        "description": description,
        "assignee": assignee,
        "assignee_name": assignee_name if assignee == "human" else "",
        "status": "pending",
        "depends_on": depends_on,
        "due": due,
        "priority": priority,
        "result": "",
        "comments": [],
        "created_at": now(),
        "created_via": created_via,
        "started_at": None,
        "completed_at": None,
        "completed_via": None,
    }


def _inbox_project(data: dict[str, Any]) -> dict[str, Any]:
    for project in data["projects"]:
        if isinstance(project, dict) and project.get("inbox"):
            return project
    timestamp = now()
    project = {
        "id": _new_id(),
        "title": INBOX_TITLE,
        "goal": "単発タスクの受け皿",
        "status": "active",
        "inbox": True,
        "created_at": timestamp,
        "updated_at": timestamp,
        "tasks": [],
    }
    data["projects"].append(project)
    return project


def _find_project(data: dict[str, Any], project_id: Any) -> tuple[dict[str, Any] | None, str]:
    projects = [p for p in data["projects"] if isinstance(p, dict)]
    wanted = str(project_id or "").strip()
    if wanted:
        for project in projects:
            if project.get("id") == wanted:
                return project, ""
        return None, f"Project not found: {wanted}"
    # Default: the single active non-inbox project; fall back to the inbox.
    active = [p for p in projects if p.get("status") == "active" and not p.get("inbox")]
    if len(active) == 1:
        return active[0], ""
    if len(active) > 1:
        listing = "; ".join(f"{p.get('id')}: {p.get('title')}" for p in active)
        return None, f"Multiple active projects. Pass project_id. Candidates: {listing}"
    inbox = next((p for p in projects if p.get("inbox")), None)
    if inbox is not None:
        return inbox, ""
    return None, "No active project. Pass project_id or create one with create_project."


def _locate_task(
    data: dict[str, Any], project_id: Any, task_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    if str(project_id or "").strip():
        project, error = _find_project(data, project_id)
        if project is None:
            return None, None, error
        for task in project.get("tasks") or []:
            if task.get("id") == task_id:
                return project, task, ""
        return None, None, f"Task {task_id} not found in project {project.get('id')}."
    matches = [
        (project, task)
        for project in data["projects"]
        if isinstance(project, dict)
        for task in project.get("tasks") or []
        if task.get("id") == task_id
    ]
    if not matches:
        return None, None, f"Task not found: {task_id}"
    if len(matches) > 1:
        ids = ", ".join(project.get("id", "?") for project, _task in matches)
        return None, None, f"Task id {task_id} exists in multiple projects ({ids}). Pass project_id."
    return matches[0][0], matches[0][1], ""


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


def _format_project(project: dict[str, Any]) -> str:
    tasks = project.get("tasks") or []
    ready = _ready_ids(project)
    done_count = sum(1 for task in tasks if task.get("status") in FINISHED_STATUSES)
    marker = " [inbox]" if project.get("inbox") else ""
    lines = [
        f"{project.get('id')} [{project.get('status')}]{marker} {project.get('title')} (done {done_count}/{len(tasks)})"
    ]
    if project.get("goal"):
        lines.append(f"goal: {project['goal']}")
    for task in tasks:
        lines.append("  " + _format_task(task, ready))
    ready_line = ", ".join(f"{task['id']}({task.get('assignee')})" for task in tasks if task["id"] in ready) or "-"
    lines.append(f"READY: {ready_line}")
    return "\n".join(lines)


def _assignee_label(task: dict[str, Any]) -> str:
    if task.get("assignee") == "human" and str(task.get("assignee_name") or "").strip():
        return str(task["assignee_name"]).strip()
    return str(task.get("assignee"))


def _format_task(task: dict[str, Any], ready_ids: set[str]) -> str:
    deps = ",".join(task.get("depends_on") or []) or "-"
    parts = [f"{task.get('id')} [{_assignee_label(task)}/{task.get('status')}] {task.get('title')} (deps: {deps})"]
    if task.get("due"):
        parts.append(f"due={task['due']}")
    if task.get("priority"):
        parts.append(f"priority={task['priority']}")
    if task.get("result"):
        result = str(task["result"])
        if len(result) > 120:
            result = result[:119] + "…"
        parts.append(f"result: {result}")
    comments = task.get("comments") or []
    if comments:
        latest = str(comments[-1].get("text", ""))
        if len(latest) > 80:
            latest = latest[:79] + "…"
        parts.append(f"comments={len(comments)} (latest: {latest})")
    if task.get("id") in ready_ids:
        parts.append("<- READY")
    return " ".join(parts)


# NOTE: keep the storage/migration helpers below in sync with scripts/viewer.py.
# The duplication is intentional: skill scripts must not import little_agent so that
# skill folders stay copy-portable.
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


def _new_id() -> str:
    return uuid4().hex[:8]


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
