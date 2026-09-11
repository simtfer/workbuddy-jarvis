"""Windows toast notifications (best effort, never blocks the caller)."""

from __future__ import annotations

import base64
import subprocess
import sys

APP_ID = "JARVIS-Win"

_SCRIPT = """
$ErrorActionPreference = "Stop"
try {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $nodes = $template.GetElementsByTagName("text")
    $nodes.Item(0).AppendChild($template.CreateTextNode({title})) > $null
    $nodes.Item(1).AppendChild($template.CreateTextNode({message})) > $null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{app_id}").Show($toast)
}} catch {{
    exit 1
}}
"""


def _ps_literal(text: str) -> str:
    cleaned = " ".join(str(text).split())[:220].replace("'", "''")
    return f"'{cleaned}'"


def notify(title: str, message: str, timeout: float = 15.0) -> bool:
    """Show a Windows toast. Returns False (silently) if unsupported."""

    if sys.platform != "win32":
        return False
    script = _SCRIPT.format(title=_ps_literal(title), message=_ps_literal(message), app_id=APP_ID)
    # -EncodedCommand avoids every .ps1 encoding trap (UTF-16LE + base64).
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001 - notifications are a nicety
        return False
    return completed.returncode == 0
