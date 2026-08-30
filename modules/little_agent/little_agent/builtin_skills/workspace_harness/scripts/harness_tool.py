"""Filesystem-based human+AI task harness.

Each task is a folder that carries its own ``task.md`` (front matter + body).
The directory tree itself is the source of truth:

    {tasks_dir}/                 harness root
      {area}/                    top-level area  -- human-controlled
        {task-slug}/             one task = one folder (AI may create here)
          task.md                task definition (front matter + notes)
          outputs/               deliverables
          notes/                 working notes
    {shared_dir}/                shared materials (searched autonomously)

Top-level *areas* are the part a human owns. The agent may create/edit task
folders inside an existing area, but it can never create, rename, or delete an
area; when it thinks a new area is needed it records a proposal instead.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

STATUSES = ("todo", "doing", "review", "blocked", "done", "cancelled", "proposed")
ASSIGNEES = ("ai", "human")
PRIORITIES = ("low", "normal", "high")
LIST_FIELDS = ("tags", "materials")
PROPOSALS_FILE = "PROPOSALS.md"
README_FILE = "README.md"
# Directory entries under the tasks root that are files/meta, not areas.
NON_AREA_ENTRIES = {PROPOSALS_FILE, README_FILE}
SEARCH_SNIPPET_MAX = 160
SEARCH_DEFAULT_LIMIT = 20
# Extensions we never try to read as text when scanning shared materials.
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip", ".gz",
    ".xlsx", ".xls", ".pptx", ".ppt", ".docx", ".doc", ".mp3", ".mp4", ".mov",
    ".exe", ".dll", ".bin", ".so", ".dylib", ".woff", ".woff2", ".ttf",
}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool") or (sys.argv[1] if len(sys.argv) > 1 else ""))
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")

        handlers = {
            "harness_overview": harness_overview,
            "list_task_folders": list_tasks,
            "read_task_folder": read_task,
            "create_task_folder": create_task,
            "update_task_folder": update_task,
            "add_task_note": add_task_note,
            "propose_area": propose_area,
            "search_shared": search_shared,
            "read_shared": read_shared,
        }
        handler = handlers.get(tool)
        if handler is None:
            result = {"ok": False, "content": f"Unknown harness tool: {tool}"}
        else:
            result = handler(workspace, arguments)
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Harness script failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# Paths / configuration
# --------------------------------------------------------------------------- #

def _resolve_dir(workspace: Path, env_name: str, default: str) -> Path:
    raw = os.getenv(env_name, default)
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def _tasks_root(workspace: Path) -> Path:
    return _resolve_dir(workspace, "LITTLE_AGENT_TASKS_DIR", "tasks")


def _shared_root(workspace: Path) -> Path:
    return _resolve_dir(workspace, "LITTLE_AGENT_SHARED_DIR", "shared")


def _display(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------- #
# Front matter (minimal, dependency-free)
# --------------------------------------------------------------------------- #

def _parse_task(text: str) -> tuple[dict[str, Any], str]:
    """Split a task.md into (front matter dict, body)."""
    meta: dict[str, Any] = {}
    body = text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.DOTALL)
    if match:
        block, body = match.group(1), match.group(2)
        for line in block.splitlines():
            if not line.strip() or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key in LIST_FIELDS:
                meta[key] = [item.strip() for item in value.split(",") if item.strip()]
            else:
                meta[key] = value
    return meta, body


def _dump_task(meta: dict[str, Any], body: str) -> str:
    order = [
        "title", "status", "assignee", "assignee_name", "priority", "due",
        "tags", "materials", "created", "updated",
    ]
    keys = order + [k for k in meta if k not in order]
    lines = ["---"]
    for key in keys:
        if key not in meta:
            continue
        value = meta[key]
        if key in LIST_FIELDS:
            value = ", ".join(value) if isinstance(value, list) else str(value)
        lines.append(f"{key}: {value}")
    lines.append("---")
    body = body.lstrip("\n")
    return "\n".join(lines) + "\n\n" + body


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slugify(title: str) -> str:
    slug = title.strip().lower()
    slug = re.sub(r"[\\/]+", "-", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r'[<>:"|?*\x00-\x1f]', "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    return slug or "task"


# --------------------------------------------------------------------------- #
# Task discovery
# --------------------------------------------------------------------------- #

def _areas(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        (child for child in root.iterdir()
         if child.is_dir() and child.name not in NON_AREA_ENTRIES and not child.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )


def _tasks_in_area(area: Path) -> list[Path]:
    return sorted(
        (child for child in area.iterdir()
         if child.is_dir() and (child / "task.md").is_file()),
        key=lambda p: p.name.lower(),
    )


def _iter_tasks(root: Path):
    for area in _areas(root):
        for task in _tasks_in_area(area):
            yield area, task


def _find_task(root: Path, ref: str) -> Path | None:
    """Resolve a task by 'area/slug' or by bare 'slug' (searched across areas)."""
    ref = ref.strip().strip("/")
    if "/" in ref:
        area_name, _, slug = ref.partition("/")
        candidate = root / area_name / slug
        return candidate if (candidate / "task.md").is_file() else None
    matches = [task for _, task in _iter_tasks(root) if task.name == ref]
    return matches[0] if len(matches) == 1 else None


def _task_meta(task: Path) -> dict[str, Any]:
    meta, _ = _parse_task((task / "task.md").read_text(encoding="utf-8"))
    return meta


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def harness_overview(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    root = _tasks_root(workspace)
    shared = _shared_root(workspace)
    lines: list[str] = [f"Tasks root: {_display(root, workspace)}"]

    if not root.exists():
        lines.append("(tasks root does not exist yet)")
        lines.append("The top-level structure is human-owned. Ask the user which")
        lines.append("areas to create, or record a suggestion with propose_area.")
    else:
        areas = _areas(root)
        if not areas:
            lines.append("(no areas yet — a human creates top-level area folders here)")
        for area in areas:
            tasks = _tasks_in_area(area)
            counts: dict[str, int] = {}
            for task in tasks:
                status = str(_task_meta(task).get("status", "todo"))
                counts[status] = counts.get(status, 0) + 1
            summary = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "empty"
            lines.append(f"  {area.name}/  ({len(tasks)} tasks — {summary})")
        proposals = root / PROPOSALS_FILE
        if proposals.is_file():
            lines.append(f"\nArea proposals awaiting a human decision: {_display(proposals, workspace)}")

    lines.append("")
    if shared.exists():
        entries = sorted(p.name for p in shared.iterdir() if not p.name.startswith("."))
        file_count = sum(1 for _ in shared.rglob("*") if _.is_file())
        top = ", ".join(entries[:12]) + ("…" if len(entries) > 12 else "")
        lines.append(f"Shared materials: {_display(shared, workspace)} ({file_count} files) [{top}]")
    else:
        lines.append(f"Shared materials: {_display(shared, workspace)} (not present)")
    return {"ok": True, "content": "\n".join(lines)}


def list_tasks(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    root = _tasks_root(workspace)
    if not root.exists():
        return {"ok": True, "content": "(no tasks root yet)"}
    area_filter = str(args.get("area") or "").strip().strip("/")
    status_filter = str(args.get("status") or "").strip()
    if status_filter and status_filter not in STATUSES:
        return {"ok": False, "content": f"Unknown status '{status_filter}'. Use one of: {', '.join(STATUSES)}."}

    rows: list[str] = []
    for area, task in _iter_tasks(root):
        if area_filter and area.name != area_filter:
            continue
        meta = _task_meta(task)
        status = str(meta.get("status", "todo"))
        if status_filter and status != status_filter:
            continue
        who = str(meta.get("assignee", "?"))
        name = str(meta.get("assignee_name") or "").strip()
        if who == "human" and name:
            who = f"human:{name}"
        due = str(meta.get("due") or "").strip()
        due_txt = f" due:{due}" if due else ""
        title = str(meta.get("title") or task.name)
        rows.append(f"{area.name}/{task.name}  [{status}] ({who}){due_txt}  {title}")
    if not rows:
        return {"ok": True, "content": "(no matching tasks)"}
    return {"ok": True, "content": "\n".join(rows)}


def read_task(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    root = _tasks_root(workspace)
    ref = str(args.get("task") or "").strip()
    if not ref:
        return {"ok": False, "content": "task (area/slug or slug) is required."}
    task = _find_task(root, ref)
    if task is None:
        return {"ok": False, "content": f"Task not found or ambiguous: '{ref}'. Use list_tasks to see exact area/slug refs."}
    content = (task / "task.md").read_text(encoding="utf-8")
    listing: list[str] = []
    for child in sorted(task.rglob("*")):
        if child.name == "task.md" or child.name == ".gitkeep":
            continue
        kind = "dir " if child.is_dir() else "file"
        listing.append(f"  {kind} {child.relative_to(task).as_posix()}")
    body = f"# {_display(task, root)}\n\n{content}"
    if listing:
        body += "\n\nFolder contents:\n" + "\n".join(listing)
    return {"ok": True, "content": body}


def create_task(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    root = _tasks_root(workspace)
    area_name = str(args.get("area") or "").strip().strip("/")
    title = str(args.get("title") or "").strip()
    if not area_name:
        return {"ok": False, "content": "area is required (an existing top-level area)."}
    if not title:
        return {"ok": False, "content": "title is required."}
    if "/" in area_name or area_name in NON_AREA_ENTRIES:
        return {"ok": False, "content": f"Invalid area name: '{area_name}'."}

    area = root / area_name
    if not area.is_dir():
        existing = [a.name for a in _areas(root)]
        hint = f" Existing areas: {', '.join(existing)}." if existing else ""
        return {
            "ok": False,
            "content": (
                f"Area '{area_name}' does not exist. Top-level areas are human-controlled, "
                f"so a new area is not created automatically. Use propose_area to suggest it, "
                f"or place the task in an existing area.{hint}"
            ),
        }

    status = str(args.get("status") or "todo").strip()
    if status not in STATUSES:
        return {"ok": False, "content": f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}."}
    assignee = str(args.get("assignee") or "ai").strip()
    if assignee not in ASSIGNEES:
        return {"ok": False, "content": f"assignee must be one of: {', '.join(ASSIGNEES)}."}
    priority = str(args.get("priority") or "normal").strip()
    if priority not in PRIORITIES:
        return {"ok": False, "content": f"priority must be one of: {', '.join(PRIORITIES)}."}

    slug = _slugify(title)
    task = area / slug
    suffix = 2
    while task.exists():
        task = area / f"{slug}-{suffix}"
        suffix += 1

    now = _now()
    meta: dict[str, Any] = {
        "title": title,
        "status": status,
        "assignee": assignee,
        "priority": priority,
        "created": now,
        "updated": now,
    }
    name = str(args.get("assignee_name") or "").strip()
    if assignee == "human" and name:
        meta["assignee_name"] = name
    for key in ("due",):
        value = str(args.get(key) or "").strip()
        if value:
            meta[key] = value
    tags = args.get("tags")
    if isinstance(tags, list):
        meta["tags"] = [str(t).strip() for t in tags if str(t).strip()]
    elif isinstance(tags, str) and tags.strip():
        meta["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

    description = str(args.get("description") or "").strip()
    body_lines = [f"# {title}", "", "## 目的", description or "(記入してください)", "", "## 進捗ログ", ""]
    (task).mkdir(parents=True)
    (task / "task.md").write_text(_dump_task(meta, "\n".join(body_lines)), encoding="utf-8")
    for sub in ("outputs", "notes"):
        (task / sub).mkdir()
        (task / sub / ".gitkeep").write_text("", encoding="utf-8")

    note = ""
    if status == "proposed":
        note = " (status=proposed: this is an AI suggestion awaiting the user's OK)"
    return {"ok": True, "content": f"Created task {_display(task, root)}{note}"}


def update_task(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    root = _tasks_root(workspace)
    ref = str(args.get("task") or "").strip()
    if not ref:
        return {"ok": False, "content": "task (area/slug or slug) is required."}
    task = _find_task(root, ref)
    if task is None:
        return {"ok": False, "content": f"Task not found or ambiguous: '{ref}'."}

    text = (task / "task.md").read_text(encoding="utf-8")
    meta, body = _parse_task(text)
    changed: list[str] = []

    if "status" in args and str(args["status"]).strip():
        status = str(args["status"]).strip()
        if status not in STATUSES:
            return {"ok": False, "content": f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}."}
        meta["status"] = status
        changed.append(f"status={status}")
    if "assignee" in args and str(args["assignee"]).strip():
        assignee = str(args["assignee"]).strip()
        if assignee not in ASSIGNEES:
            return {"ok": False, "content": f"assignee must be one of: {', '.join(ASSIGNEES)}."}
        meta["assignee"] = assignee
        if assignee == "ai":
            meta.pop("assignee_name", None)
        changed.append(f"assignee={assignee}")
    if "assignee_name" in args:
        name = str(args["assignee_name"]).strip()
        if name and meta.get("assignee") == "human":
            meta["assignee_name"] = name
            changed.append(f"assignee_name={name}")
        elif not name:
            meta.pop("assignee_name", None)
            changed.append("assignee_name cleared")
    if "priority" in args and str(args["priority"]).strip():
        priority = str(args["priority"]).strip()
        if priority not in PRIORITIES:
            return {"ok": False, "content": f"priority must be one of: {', '.join(PRIORITIES)}."}
        meta["priority"] = priority
        changed.append(f"priority={priority}")
    if "due" in args:
        due = str(args["due"]).strip()
        if due:
            meta["due"] = due
            changed.append(f"due={due}")
        else:
            meta.pop("due", None)
            changed.append("due cleared")
    if "tags" in args:
        tags = args["tags"]
        values = tags if isinstance(tags, list) else str(tags).split(",")
        meta["tags"] = [str(t).strip() for t in values if str(t).strip()]
        changed.append("tags updated")
    if "materials_add" in args and str(args["materials_add"]).strip():
        current = meta.get("materials")
        if not isinstance(current, list):
            current = []
        add = str(args["materials_add"]).strip()
        if add not in current:
            current.append(add)
            changed.append(f"linked material {add}")
        meta["materials"] = current

    if not changed:
        return {"ok": False, "content": "No recognized fields to update."}
    meta["updated"] = _now()
    (task / "task.md").write_text(_dump_task(meta, body), encoding="utf-8")
    return {"ok": True, "content": f"Updated {_display(task, root)}: " + "; ".join(changed)}


def add_task_note(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    root = _tasks_root(workspace)
    ref = str(args.get("task") or "").strip()
    text_note = str(args.get("text") or "").strip()
    if not ref:
        return {"ok": False, "content": "task (area/slug or slug) is required."}
    if not text_note:
        return {"ok": False, "content": "text is required."}
    task = _find_task(root, ref)
    if task is None:
        return {"ok": False, "content": f"Task not found or ambiguous: '{ref}'."}

    via = str(args.get("via") or "agent").strip() or "agent"
    meta, body = _parse_task((task / "task.md").read_text(encoding="utf-8"))
    entry = f"- {_now()} ({via}) {text_note}"
    if "## 進捗ログ" in body:
        body = body.rstrip() + "\n" + entry + "\n"
    else:
        body = body.rstrip() + "\n\n## 進捗ログ\n\n" + entry + "\n"
    meta["updated"] = _now()
    (task / "task.md").write_text(_dump_task(meta, body), encoding="utf-8")
    return {"ok": True, "content": f"Added progress note to {_display(task, root)}"}


def propose_area(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    root = _tasks_root(workspace)
    name = str(args.get("name") or "").strip().strip("/")
    reason = str(args.get("reason") or "").strip()
    if not name:
        return {"ok": False, "content": "name (proposed area name) is required."}
    if "/" in name:
        return {"ok": False, "content": "An area name cannot contain '/'."}
    if (root / name).is_dir():
        return {"ok": False, "content": f"Area '{name}' already exists — no proposal needed."}

    root.mkdir(parents=True, exist_ok=True)
    proposals = root / PROPOSALS_FILE
    header = "" if proposals.exists() else "# Area proposals\n\nTop-level areas are created by a human. The agent records suggestions here.\n"
    entry = f"\n## {name}\n- proposed: {_now()}\n- reason: {reason or '(none given)'}\n- status: awaiting human decision\n"
    with proposals.open("a", encoding="utf-8") as handle:
        if header:
            handle.write(header)
        handle.write(entry)
    return {
        "ok": True,
        "content": (
            f"Recorded a proposal for a new area '{name}' in {_display(proposals, workspace)}. "
            f"The folder was NOT created — a human decides top-level structure. "
            f"Tell the user so they can create tasks/{name}/ if they agree."
        ),
    }


def search_shared(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    shared = _shared_root(workspace)
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "content": "query is required."}
    if not shared.exists():
        return {"ok": True, "content": f"Shared materials folder not found: {_display(shared, workspace)}"}
    try:
        limit = int(args.get("limit") or SEARCH_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = SEARCH_DEFAULT_LIMIT
    needle = query.lower()

    name_hits: list[str] = []
    content_hits: list[str] = []
    for path in sorted(shared.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        rel = path.relative_to(shared).as_posix()
        if needle in rel.lower():
            name_hits.append(f"[name] {rel}")
        if path.suffix.lower() in BINARY_EXTS:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for index, line in enumerate(lines, start=1):
            if needle in line.lower():
                snippet = line.strip()[:SEARCH_SNIPPET_MAX]
                content_hits.append(f"[text] {rel}:{index}: {snippet}")
                break

    hits = (name_hits + content_hits)[:limit]
    if not hits:
        return {"ok": True, "content": f"No shared materials matched '{query}' under {_display(shared, workspace)}."}
    head = f"Shared matches for '{query}' (root {_display(shared, workspace)}):"
    return {"ok": True, "content": head + "\n" + "\n".join(hits)}


def read_shared(workspace: Path, args: dict[str, Any]) -> dict[str, Any]:
    shared = _shared_root(workspace)
    rel = str(args.get("path") or "").strip()
    if not rel:
        return {"ok": False, "content": "path (relative to the shared root) is required."}
    target = (shared / rel).resolve()
    if shared not in [target, *target.parents]:
        return {"ok": False, "content": "Path is outside the shared materials folder."}
    if not target.is_file():
        return {"ok": False, "content": f"Shared file not found: {rel}"}
    if target.suffix.lower() in BINARY_EXTS:
        return {"ok": False, "content": f"'{rel}' is a binary/office file; use a dedicated skill (excel_file, ppt_file) to read it."}
    try:
        return {"ok": True, "content": target.read_text(encoding="utf-8")}
    except UnicodeDecodeError:
        return {"ok": False, "content": f"'{rel}' is not UTF-8 text."}


if __name__ == "__main__":
    raise SystemExit(main())
