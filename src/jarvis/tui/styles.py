"""Stylesheet for the JARVIS app.

Split out of ``app.py`` so the layout rules live next to the widgets they
style. The sidebar geometry lives in ``widgets.py`` (the rows are padded to
those numbers), so the CSS widths are spliced in rather than typed twice.
"""

from __future__ import annotations

from .widgets import MENU_RAIL_WIDTH, MENU_WIDTH

CSS = """
#body { height: 1fr; }
#menu {
    width: __MENU_WIDTH__; padding: 0 1; border-right: solid $panel;
    color: $text-muted; background: transparent;
}
#menu.collapsed { width: __MENU_RAIL_WIDTH__; padding: 0 0; }
#menu-rail { display: none; color: $accent; padding: 1 0 0 1; }
#menu.collapsed #menu-rail { display: block; }
#menu-box { height: 1fr; }
#menu.collapsed #menu-box { display: none; }
/* Collapse / expand handles: the rail is the click target when the sidebar is
   closed, the head when it is open. Hover feedback so they read as clickable. */
#menu-rail:hover { background: $panel 40%; }
#menu-head { color: $text-muted; padding: 0 0 1 0; }
#menu-head:hover { background: $panel 40%; }
#menu-list {
    height: 1fr; border: none; padding: 0; background: transparent;
}
#menu-list:focus { border: none; }
#chat { width: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
#side {
    width: 46; padding: 0 1; border-left: solid $panel; color: $text-muted;
}
#side.hidden { display: none; }
/* The panel's click target while it is collapsed: #side is display:none, so
   the handle lives outside it, as a thin strip on the far right edge. */
#side-rail { width: 3; padding: 1 0 0 0; color: $accent; }
#side-rail.hidden { display: none; }
#side-rail:hover { background: $panel 40%; }
#side-head { color: $accent; padding: 0 0 1 0; }
#side-head:hover { background: $panel 40%; }
#banner { color: $accent; padding: 1 0 0 0; }
.user-message {
    color: $text; background: $primary 25%; border-left: thick $primary;
    padding: 0 1; margin: 1 0 0 0;
}
.assistant-message { margin: 1 0 0 0; padding: 0 1; background: transparent; }
.thinking {
    margin: 1 0 0 0; padding: 0 1; border-left: solid $panel;
    background: $panel 20%;
}
/* Collapsed blocks sit flush under whatever is above (tool rows, another
   thinking block); only an expanded block keeps the breathing room. */
.thinking.-collapsed { margin-top: 0; padding-bottom: 0; }
.thinking-body { color: $text-muted; padding: 0 0 1 0; }
.tool-call { margin-top: 1; background: transparent; border-top: none; }
/* Collapsed tool blocks sit directly under each other - Collapsible's
   default padding-bottom would leave a blank line between two of them.
   An expanded block keeps the breathing room via the base rule above. */
.tool-call.-collapsed { margin-top: 0; padding-bottom: 0; }
.tool-call CollapsibleTitle { color: $warning; background: transparent; padding: 0 1; }
.tool-call.bad CollapsibleTitle { color: $error; }
.tool-body { color: $text-muted; padding: 0 0 1 0; }
/* Sub-agent board: same flush-collapsed rule as the other blocks. */
.subagent-board { margin-top: 1; background: transparent; border-top: none; }
.subagent-board.-collapsed { margin-top: 0; padding-bottom: 0; }
.subagent-board CollapsibleTitle { color: $accent; background: transparent; padding: 0 1; }
.subagent-board.bad CollapsibleTitle { color: $error; }
.board-body { color: $text-muted; padding: 0 0 0 0; }
.subagent-row { padding: 0 1; color: $text; }
.subagent-row:hover { background: $panel 40%; }
.notice { color: $text-muted; padding: 0 1; margin-top: 1; }
.notice.bad { color: $error; }
.notice.warn { color: $warning; }
.plan-view {
    color: $accent; border: round $accent 40%; padding: 0 1; margin-top: 1;
}
#prompt { dock: bottom; }
""".replace("__MENU_WIDTH__", str(MENU_WIDTH)).replace(
    "__MENU_RAIL_WIDTH__", str(MENU_RAIL_WIDTH)
)
