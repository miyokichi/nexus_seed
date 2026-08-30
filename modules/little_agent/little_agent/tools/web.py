from __future__ import annotations

import socket
import urllib.error
import urllib.request

from little_agent.tools.base import ToolContext, ToolResult


_DEFAULT_MAX_BYTES = 1_000_000
_USER_AGENT = "little-agent/0.1"


class FetchUrlTool:
    name = "fetch_url"
    description = "Fetch the text content of an http(s) URL and return it (truncated if large)."
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to fetch."},
            "timeout_seconds": {"type": "integer", "description": "Timeout in seconds.", "default": 30},
            "max_bytes": {
                "type": "integer",
                "description": "Maximum number of bytes to read from the response body.",
                "default": _DEFAULT_MAX_BYTES,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, **kwargs: object) -> ToolResult:
        url = str(kwargs["url"]).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolResult(False, f"Only http(s) URLs are supported: {url}")
        timeout = int(kwargs.get("timeout_seconds", 30))
        max_bytes = int(kwargs.get("max_bytes", _DEFAULT_MAX_BYTES))

        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
                charset = response.headers.get_content_charset() or "utf-8"
                status = response.status
        except urllib.error.HTTPError as exc:
            return ToolResult(False, f"HTTP {exc.code} for {url}: {exc.reason}")
        except urllib.error.URLError as exc:
            return ToolResult(False, f"Could not reach {url}: {exc.reason}")
        except (TimeoutError, socket.timeout):
            return ToolResult(False, f"Request to {url} timed out after {timeout} seconds.")

        truncated = len(raw) > max_bytes
        body = raw[:max_bytes].decode(charset, errors="replace")
        if truncated:
            body = f"{body}\n... (truncated at {max_bytes} bytes)"
        return ToolResult(True, f"[HTTP {status}]\n{body}")
