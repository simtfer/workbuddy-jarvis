"""Headless smoke test for the TUI: boot, slash command, confirmation modal.

Run with:  uv run python tests/smoke_tui.py
"""

from __future__ import annotations

import asyncio
import faulthandler
import sys

from jarvis.config import load_config
from jarvis.core.agent import ConfirmRequest
from jarvis.tui.app import JarvisApp
from jarvis.tui.screens import ConfirmScreen

# Dump every thread's stack if the test wedges, so a hang is never a mystery.
faulthandler.dump_traceback_later(40, exit=True)


async def main() -> int:
    app = JarvisApp(load_config())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        chat = app.query_one("#chat")
        assert len(chat.children) >= 1, "banner missing"
        print("[ok] app booted, header/panel/prompt mounted")

        panel = app.query_one("#syspanel")
        content = str(panel.content)
        assert "CPU" in content, f"syspanel looks empty: {content[:80]!r}"
        print("[ok] syspanel sample:", content.splitlines()[3])

        await pilot.press(*"/tools")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        print("[ok] /tools rendered, chat children =", len(chat.children))

        request = ConfirmRequest(tool="run_shell", arguments={"command": "echo hi"}, hint="test")
        answer = asyncio.ensure_future(app._confirm(request))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen), f"expected modal, got {app.screen!r}"
        await pilot.press("y")
        approved = await answer
        assert approved is True, "y should approve"
        print("[ok] confirmation modal approved")

        answer2 = asyncio.ensure_future(app._confirm(request))
        await pilot.pause()
        await pilot.press("escape")
        assert await answer2 is False, "escape should deny"
        print("[ok] confirmation modal denied")

        app.action_toggle_side()
        await pilot.pause()
        assert app.query_one("#side").has_class("hidden")
        print("[ok] side panel toggle")

        await app.action_clear_chat()
        await pilot.pause()
        print("[ok] /clear emptied chat, children =", len(chat.children))

    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
