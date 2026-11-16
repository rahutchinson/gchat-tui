from __future__ import annotations

import logging
import time
import zlib
from datetime import date, timedelta

from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label, ListItem, ListView, RichLog, Static

from .api import ChatClient, Conversation, Message
from .session import AuthError, Session

log = logging.getLogger(__name__)

POLL_SECONDS = 4
MAX_NICK_WIDTH = 16
# Plain ANSI color names: rendered with *your terminal's* palette.
NICK_COLORS = [
    "cyan", "magenta", "green", "yellow", "blue", "red",
    "bright_cyan", "bright_magenta", "bright_green", "bright_yellow", "bright_blue", "bright_red",
]


def nick_color(user_id: str) -> str:
    return NICK_COLORS[zlib.crc32(user_id.encode()) % len(NICK_COLORS)]


def day_label(d: date) -> str:
    today = date.today()
    if d == today:
        return "today"
    if d == today - timedelta(days=1):
        return "yesterday"
    return d.strftime("%a %b %-d, %Y" if d.year != today.year else "%a %b %-d")


class ConvItem(ListItem):
    def __init__(self, conv: Conversation, title: str):
        self.label = Label("")
        super().__init__(self.label)
        self.conv = conv
        self.title = title
        self.refresh_label()

    def refresh_label(self) -> None:
        c = self.conv
        t = Text(no_wrap=True, overflow="ellipsis")
        if c.unread:
            t.append("● ", style="bold green")
            t.append(self.title, style="bold")
        else:
            t.append("★ " if c.starred else "  ", style="yellow")
            t.append(self.title)
        self.label.update(t)


class GChatApp(App):
    TITLE = "gchat"
    CSS = """
    Screen { background: ansi_default; layout: vertical; }

    #main { height: 1fr; }

    #sidebar {
        width: 32;
        border: round ansi_bright_black;
        border-title-color: ansi_bright_black;
        padding: 0;
    }
    #sidebar:focus-within { border: round ansi_blue; border-title-color: ansi_blue; }
    #convs { background: ansi_default; }
    #convs > ListItem { padding: 0 1; background: ansi_default; }
    #convs Label { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    #convs, #log {
        scrollbar-size-vertical: 1;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-color: ansi_bright_black;
        scrollbar-color-hover: ansi_white;
        scrollbar-color-active: ansi_blue;
    }
    #convs > ListItem.-highlight { background: ansi_bright_black; color: ansi_default; text-style: none; }
    #convs:focus > ListItem.-highlight { background: ansi_blue; color: ansi_black; text-style: bold; }

    #chat {
        width: 1fr;
        border: round ansi_bright_black;
        border-title-color: ansi_default;
        border-title-style: bold;
        border-subtitle-color: ansi_bright_black;
    }
    #chat:focus-within { border: round ansi_blue; }
    #log { background: ansi_default; padding: 0 1; }

    #prompt-row { height: 1; margin: 0 1; }
    #prompt { width: 2; color: ansi_blue; text-style: bold; }
    #compose { border: none; height: 1; padding: 0; background: ansi_default; }
    #compose:focus { border: none; }
    #compose > .input--placeholder { color: ansi_bright_black; }

    #statusbar { height: 1; background: ansi_blue; color: ansi_black; }
    #status-left { width: 1fr; padding: 0 1; }
    #status-right { width: auto; padding: 0 1; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("escape", "focus_list", "chats"),
        Binding("ctrl+r", "refresh", "refresh"),
        Binding("pageup", "scroll_log(-1)", show=False),
        Binding("pagedown", "scroll_log(1)", show=False),
    ]

    def __init__(self, session: Session | None = None, client: ChatClient | None = None):
        super().__init__(ansi_color=True)
        self.theme = "ansi-dark"
        self.session = session or Session()
        self.client = client
        self.convs: dict[str, Conversation] = {}
        self.current: Conversation | None = None
        self._msgs: list[Message] = []
        self._nick_width = 8
        self._polling = False
        self._next_recover = 0.0
        self._recover_backoff = 15
        self._state = "starting browser…"
        # terminal tab title: "(N) Google Chat", flashing while unfocused with new messages
        self._term_focused = True
        self._flash_text: str | None = None
        self._flash_phase = False
        self._last_title: str | None = None

    # ---- layout ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield ListView(id="convs")
            with Vertical(id="chat"):
                yield RichLog(id="log", wrap=True, markup=False, auto_scroll=True, min_width=20)
                with Horizontal(id="prompt-row"):
                    yield Static("›", id="prompt")
                    yield Input(placeholder="select a chat with ↑↓ and enter", id="compose", disabled=True)
        with Horizontal(id="statusbar"):
            yield Static("", id="status-left")
            yield Static("", id="status-right")

    def on_mount(self) -> None:
        self.query_one("#sidebar").border_title = "chats"
        self.query_one("#chat").border_title = "gchat"
        if self._driver is not None:
            self._driver.write("\x1b[22;0t")  # push the current title so we can restore it
        self._write_title("Google Chat")
        self.set_interval(1.0, self._flash_tick)
        self._log_note("connecting to Google Chat…")
        self.update_status()
        self.connect()

    def update_status(self, state: str | None = None) -> None:
        if state is not None:
            self._state = state
        n = sum(c.unread for c in self.convs.values())
        left = Text()
        left.append(" gchat ", style="bold reverse")
        left.append(f" {self._state}")
        if n:
            left.append(f"  ● {n} unread", style="bold")
        right = Text("^q quit  esc chats  ^r refresh  pgup/pgdn scroll")
        self.query_one("#status-left", Static).update(left)
        self.query_one("#sidebar").border_subtitle = f"{n} unread" if n else ""
        self.query_one("#status-right", Static).update(right)

    # ---- terminal tab title ----------------------------------------------------
    def _write_title(self, title: str) -> None:
        # Names come from other users; drop control chars (ESC, BEL, C1...) so a chat name
        # can't inject terminal escape sequences.
        title = "".join(ch for ch in title if ch.isprintable())[:80]
        if title != self._last_title and self._driver is not None:
            self._driver.write(f"\x1b]0;{title}\x07")  # OSC 0: set tab/window title
            self._last_title = title

    def _base_title(self) -> str:
        n = sum(c.unread for c in self.convs.values())
        return f"({n}) Google Chat" if n else "Google Chat"

    def update_term_title(self) -> None:
        if self._flash_text and self._flash_phase:
            self._write_title(self._flash_text)
        else:
            self._write_title(self._base_title())

    def _flash_tick(self) -> None:
        if self._flash_text:
            self._flash_phase = not self._flash_phase
            self.update_term_title()

    def alert(self, sender: str) -> None:
        """New incoming message: flash the tab if the terminal isn't focused."""
        self.bell()
        if not self._term_focused:
            self._flash_text = f"💬 {sender}"
            self._flash_phase = True
        self.update_term_title()

    def on_app_focus(self, _: events.AppFocus) -> None:
        self._term_focused = True
        self._flash_text = None
        self.update_term_title()
        if self.current is not None and self.current.unread and self.client is not None:
            self.run_worker(self.mark_current_read(), group="markread")

    def on_app_blur(self, _: events.AppBlur) -> None:
        self._term_focused = False

    # ---- startup / polling ---------------------------------------------------
    @work(exclusive=True, group="connect")
    async def connect(self) -> None:
        try:
            if self.client is None:
                await self.session.start()
                self.client = ChatClient(self.session)
            await self.refresh_conversations(initial=True)
        except AuthError as e:
            self._log_note(f"login failed: {e}", style="red")
            self.update_status("not connected")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("connect failed")
            self._log_note(f"error: {e}", style="red")
            self.update_status("not connected")
            return
        self.query_one("#log", RichLog).clear()
        self._log_note("pick a chat on the left: ↑↓ to move, enter to open")
        self.update_status("connected")
        self.query_one("#convs", ListView).focus()
        self.set_interval(POLL_SECONDS, self.poll)

    async def refresh_conversations(self, initial: bool = False) -> list[Conversation]:
        """Fetch the conversation list; return conversations with new activity."""
        fresh = await self.client.list_conversations(50)
        changed = []
        for c in fresh:
            old = self.convs.get(c.key)
            if (old is None and self.convs and c.unread) or (old is not None and c.sort_ts > old.sort_ts):
                changed.append(c)
        if self.current and self.current.key in {c.key for c in fresh}:
            # keep our local read marker for the open conversation
            cur = next(c for c in fresh if c.key == self.current.key)
            cur.last_read = max(cur.last_read, self.current.last_read)
            self.current = cur
        self.convs = {c.key: c for c in fresh}
        await self._render_list(fresh)
        self.update_term_title()
        self.update_status()
        return [] if initial else changed

    async def _render_list(self, convs: list[Conversation]) -> None:
        lv = self.query_one("#convs", ListView)
        items = [i for i in lv.children if isinstance(i, ConvItem)]
        if [i.conv.key for i in items] == [c.key for c in convs]:
            for item, c in zip(items, convs):  # same order: update in place
                item.conv = c
                item.title = self.client.display_name(c)
                item.refresh_label()
            return
        hl = lv.highlighted_child.conv.key if isinstance(lv.highlighted_child, ConvItem) else None
        await lv.clear()
        await lv.extend(ConvItem(c, self.client.display_name(c)) for c in convs)
        lv.index = next((n for n, c in enumerate(convs) if c.key == hl), 0)

    async def _recover(self) -> None:
        now = time.monotonic()
        if now < self._next_recover:
            return
        self.update_status("session expired, reconnecting…")
        try:
            await self.session.recover()
        except Exception as e:  # noqa: BLE001 - never let recovery kill the app
            log.warning("recover failed: %s", e)
            self._recover_backoff = min(self._recover_backoff * 2, 600)
            self._next_recover = now + self._recover_backoff
            self.update_status(f"signed out; retrying in {self._recover_backoff}s (see log)")
        else:
            self._recover_backoff = 15
            self.update_status("connected")

    async def poll(self) -> None:
        if self._polling or self.client is None or time.monotonic() < self._next_recover:
            return
        self._polling = True
        try:
            changed = await self.refresh_conversations()
            if self._state != "connected":
                self.update_status("connected")
            for c in changed:
                if self.current and c.key == self.current.key:
                    # only mark read if the user can actually see it
                    msgs = await self.load_messages(mark_read=self._term_focused)
                    if msgs and msgs[-1].creator_id != self.client.me:
                        self.alert(msgs[-1].creator_name or self.client.display_name(c))
                elif c.unread:
                    name = self.client.display_name(c)
                    self.notify(f"new message in {name}", timeout=6, markup=False)
                    self.alert(name)
        except AuthError as e:
            log.warning("auth error during poll: %s", e)
            await self._recover()
        except Exception as e:  # noqa: BLE001
            log.warning("poll failed: %s", e)
            self.update_status(f"⚠ {type(e).__name__}, retrying")
        finally:
            self._polling = False

    # ---- conversations ---------------------------------------------------------
    @on(ListView.Selected, "#convs")
    def open_selected(self, ev: ListView.Selected) -> None:
        if not isinstance(ev.item, ConvItem):
            return
        self.current = ev.item.conv
        name = self.client.display_name(self.current)
        chat = self.query_one("#chat")
        chat.border_title = name
        n = len(self.current.member_ids)
        chat.border_subtitle = "dm" if self.current.is_dm and n <= 2 else (f"{n} people" if n else "space")
        inp = self.query_one("#compose", Input)
        inp.disabled = False
        inp.placeholder = f"message {name}"
        inp.focus()
        self._msgs = []
        self.query_one("#log", RichLog).clear()
        self._log_note("loading…")
        self.open_conversation()

    @work(exclusive=True, group="messages")
    async def open_conversation(self) -> None:
        await self.load_messages(mark_read=True)

    async def load_messages(self, mark_read: bool = False) -> list[Message]:
        conv = self.current
        if conv is None:
            return []
        try:
            msgs = await self.client.list_messages(conv)
        except Exception as e:  # noqa: BLE001
            log.exception("list_messages failed")
            self.notify(f"couldn't load messages: {e}", severity="error", markup=False)
            return []
        if self.current is None or conv.key != self.current.key:
            return msgs  # user switched away while loading
        self._msgs = msgs
        self._render_messages()
        if mark_read:
            await self.mark_current_read()
        return msgs

    async def mark_current_read(self) -> None:
        conv = self.current
        if conv is None or not conv.unread:
            return
        try:
            await self.client.mark_read(conv)
        except Exception as e:  # noqa: BLE001
            log.warning("mark_read failed: %s", e)
            return
        if conv.key in self.convs:
            self.convs[conv.key].last_read = conv.last_read
        for item in self.query_one("#convs", ListView).children:
            if isinstance(item, ConvItem) and item.conv.key == conv.key:
                item.conv.last_read = conv.last_read
                item.refresh_label()
        self.update_term_title()
        self.update_status()

    # ---- message rendering ---------------------------------------------------------
    def _log_note(self, text: str, style: str = "bright_black") -> None:
        self.query_one("#log", RichLog).write(Text(text, style=style))

    def _nick(self, m: Message) -> Text:
        if m.creator_id == self.client.me:
            name = self.client.first_names.get(m.creator_id) or "me"
            return Text(name, style="bold")
        name = self.client.first_names.get(m.creator_id) or (m.creator_name.split() or ["?"])[0]
        return Text(name, style=f"bold {nick_color(m.creator_id)}")

    def _line(self, time: str, nick: Text | str, body: Text) -> Table:
        """One irssi-style row: `HH:MM     nick │ message`, message wraps in its column."""
        g = Table.grid(padding=(0, 1), expand=True)
        g.add_column(width=5, style="bright_black", no_wrap=True)
        g.add_column(width=self._nick_width, justify="right", no_wrap=True, overflow="ellipsis")
        g.add_column(width=1, style="bright_black")
        g.add_column(ratio=1, overflow="fold")
        g.add_row(time, nick, "│", body)
        return g

    def _render_messages(self) -> None:
        rl = self.query_one("#log", RichLog)
        rl.clear()
        msgs = self._msgs
        if not msgs:
            self._log_note("no messages yet")
            return
        width = rl.scrollable_content_region.width or None
        longest = max(len(self._nick(m).plain) for m in msgs)
        self._nick_width = max(4, min(MAX_NICK_WIDTH, longest))
        prev: Message | None = None
        rows = []
        for m in msgs:
            if prev is None or prev.time.date() != m.time.date():
                rows.append(Rule(day_label(m.time.date()), style="bright_black", characters="─"))
                prev = None
            same_block = (
                prev is not None
                and prev.creator_id == m.creator_id
                and m.time - prev.time <= timedelta(minutes=5)
            )
            body = Text()
            if m.is_reply:
                body.append("↳ ", style="bright_black")
            body.append(m.text or "")
            for label in m.attachments:
                if body.plain.strip():
                    body.append(" ")
                body.append(f"[{label}]", style="italic cyan")
            if not m.text and not m.attachments:
                body.append("[unsupported message]", style="italic bright_black")
            time = "" if same_block and m.time.strftime("%H:%M") == prev.time.strftime("%H:%M") else m.time.strftime("%H:%M")
            rows.append(self._line(time, Text("") if same_block else self._nick(m), body))
            prev = m
        rl.write(Group(*rows), width=width, expand=True)

    def on_resize(self, _: events.Resize) -> None:
        if self._msgs:
            self.call_after_refresh(self._render_messages)

    # ---- sending -----------------------------------------------------------------
    @on(Input.Submitted, "#compose")
    def submit(self, ev: Input.Submitted) -> None:
        text = ev.value.strip()
        if not text or self.current is None:
            return
        ev.input.value = ""
        rl = self.query_one("#log", RichLog)
        rl.write(self._line("", Text("…", style="bright_black"), Text(text, style="bright_black")),
                 width=rl.scrollable_content_region.width or None, expand=True)
        self.send_message(self.current, text)

    @work(group="send")
    async def send_message(self, conv: Conversation, text: str) -> None:
        try:
            await self.client.send(conv, text)
        except Exception as e:  # noqa: BLE001
            log.exception("send failed")
            self.notify(f"send failed: {e}", severity="error", timeout=10, markup=False)
            inp = self.query_one("#compose", Input)
            if not inp.value:
                inp.value = text
        if self.current and self.current.key == conv.key:
            await self.load_messages()

    # ---- actions -----------------------------------------------------------------
    def action_focus_list(self) -> None:
        self.query_one("#convs", ListView).focus()

    def action_refresh(self) -> None:
        self.run_worker(self.poll(), exclusive=False)
        if self.current:
            self.open_conversation()

    def action_scroll_log(self, direction: int) -> None:
        rl = self.query_one("#log", RichLog)
        (rl.scroll_page_up if direction < 0 else rl.scroll_page_down)()

    async def on_unmount(self) -> None:
        if self._driver is not None:
            self._driver.write("\x1b[23;0t")  # pop: restore the title from before we started
        try:
            await self.session.close()
        except Exception:  # noqa: BLE001
            pass
