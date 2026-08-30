from __future__ import annotations

import csv
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class EmailItem:
    sender_name: str
    sender_email: str
    recipients: str
    subject: str
    sent_on: str
    body_preview: str
    has_attachments: bool
    attachment_count: int
    item_class: str


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        workspace = Path(str(payload["workspace"])).resolve()
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object.")
        result = run(workspace, arguments)
    except Exception as exc:  # noqa: BLE001 - serialized for the host agent.
        result = {"ok": False, "content": f"Outlook email extraction failed: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0


def run(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    source_type = str(arguments.get("source_type") or "live")
    output_format = str(arguments.get("output_format") or "csv")
    output_path = safe_output_path(workspace, str(arguments.get("output_path") or "output_emails.csv"), output_format)
    pst_path = arguments.get("pst_path")
    if source_type == "pst" and not pst_path:
        return {"ok": False, "content": "pst_path is required when source_type is pst."}

    try:
        emails = extract_emails(
            source_type=source_type,
            pst_path=str(pst_path) if pst_path else None,
            subject_filter=optional_text(arguments.get("subject_filter")),
            sender_filter=optional_text(arguments.get("sender_filter")),
            include_body=bool(arguments.get("include_body", False)),
        )
    except ImportError:
        return {"ok": False, "content": "pywin32 is required. Install it with: pip install pywin32"}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        output_path.write_text(
            json.dumps([asdict(email) for email in emails], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        write_csv(output_path, emails, include_body=bool(arguments.get("include_body", False)))

    return {"ok": True, "content": f"Wrote {output_path.relative_to(workspace)} ({len(emails)} emails)"}


def extract_emails(
    source_type: str,
    pst_path: str | None,
    subject_filter: str | None,
    sender_filter: str | None,
    include_body: bool,
) -> list[EmailItem]:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        roots = outlook.Folders
        if source_type == "pst":
            pst = str(Path(pst_path or "").expanduser().resolve())
            store = find_or_add_pst(outlook, pst)
            roots = [store.GetRootFolder()]

        emails: list[EmailItem] = []
        for folder in roots:
            walk_folder(folder, emails, subject_filter, sender_filter, include_body)
        return emails
    finally:
        pythoncom.CoUninitialize()


def find_or_add_pst(outlook: Any, pst_path: str) -> Any:
    for store in outlook.Stores:
        if getattr(store, "FilePath", "").lower() == pst_path.lower():
            return store
    outlook.AddStore(pst_path)
    for store in outlook.Stores:
        if getattr(store, "FilePath", "").lower() == pst_path.lower():
            return store
    raise RuntimeError(f"Could not mount pst file: {pst_path}")


def walk_folder(
    folder: Any,
    emails: list[EmailItem],
    subject_filter: str | None,
    sender_filter: str | None,
    include_body: bool,
) -> None:
    try:
        items = folder.Items
        items.Sort("[ReceivedTime]", True)
        for item in items:
            email = extract_item(item, include_body)
            if email and matches(email, subject_filter, sender_filter):
                emails.append(email)
    except Exception:
        pass

    try:
        for child in folder.Folders:
            walk_folder(child, emails, subject_filter, sender_filter, include_body)
    except Exception:
        pass


def extract_item(item: Any, include_body: bool) -> EmailItem | None:
    try:
        item_class = safe_str(item.MessageClass)
        if not item_class.startswith("IPM.Note"):
            return None
        sender_name = safe_str(item.SenderName)
        sender_email = sender_address(item)
        recipients = ", ".join(safe_str(recipient.Name) for recipient in item.Recipients)
        body = safe_str(item.Body)
        return EmailItem(
            sender_name=sender_name,
            sender_email=sender_email,
            recipients=recipients,
            subject=safe_str(item.Subject),
            sent_on=format_date(item.SentOn),
            body_preview=body[:500] if include_body else "",
            has_attachments=item.Attachments.Count > 0,
            attachment_count=item.Attachments.Count,
            item_class=item_class,
        )
    except Exception:
        return None


def sender_address(item: Any) -> str:
    try:
        return safe_str(item.SenderEmailAddress)
    except Exception:
        pass
    try:
        exchange_user = item.Sender.GetExchangeUser()
        if exchange_user:
            return safe_str(exchange_user.PrimarySmtpAddress)
    except Exception:
        pass
    return ""


def matches(email: EmailItem, subject_filter: str | None, sender_filter: str | None) -> bool:
    if subject_filter and subject_filter.lower() not in email.subject.lower():
        return False
    if sender_filter:
        needle = sender_filter.lower()
        if needle not in email.sender_name.lower() and needle not in email.sender_email.lower():
            return False
    return True


def write_csv(path: Path, emails: list[EmailItem], include_body: bool) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        header = [
            "sender_name",
            "sender_email",
            "recipients",
            "subject",
            "sent_on",
            "has_attachments",
            "attachment_count",
        ]
        if include_body:
            header.append("body_preview")
        writer.writerow(header)
        for email in emails:
            row = [
                email.sender_name,
                email.sender_email,
                email.recipients,
                email.subject,
                email.sent_on,
                email.has_attachments,
                email.attachment_count,
            ]
            if include_body:
                row.append(email.body_preview)
            writer.writerow(row)


def safe_output_path(workspace: Path, requested: str, output_format: str) -> Path:
    path = Path(requested)
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve()
    if workspace not in [resolved, *resolved.parents]:
        raise ValueError("output_path escaped the workspace.")
    expected_suffix = ".json" if output_format == "json" else ".csv"
    if resolved.suffix.lower() != expected_suffix:
        resolved = resolved.with_suffix(expected_suffix)
    return resolved


def optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def safe_str(value: object) -> str:
    return "" if value is None else str(value)


def format_date(value: object) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else safe_str(value)


if __name__ == "__main__":
    raise SystemExit(main())
