from __future__ import annotations

import logging
import time
import zlib
from datetime import date, timedelta

from rich.box import Box
from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Label, ListItem, ListView, Static

from .api import ChatClient, Conversation, Message
from .session import AuthError, Session
from .stream import EVENT_TYPING, TYPING, parse_frame

log = logging.getLogger(__name__)

POLL_FAST = 4  # seconds between polls when the live stream is quiet
POLL_SLOW = 30  # fallback poll interval while the stream is delivering events
STREAM_FRESH = 45  # stream counts as healthy if a frame arrived within this many seconds
TYPING_TTL = 7  # forget a "typing" signal after this long without an update
TYPING_RESEND = 5  # while we type, re-send our typing state this often
TYPING_IDLE = 6  # send "stopped" after this long without a keystroke
MAX_NICK_WIDTH = 16
THUMBS, HEART = "👍", "❤️"
# Table borders: nothing but a vertical divider between the name and message columns.
DIVIDER = Box("    \n  │ \n    \n  │ \n    \n    \n  │ \n    \n")
DIVIDER_SELECTED = Box("    \n  ┃ \n    \n  ┃ \n    \n    \n  ┃ \n    \n")
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


def same_emoji(a: str, b: str) -> bool:
    return a.replace("️", "") == b.replace("️", "")


def snippet(text: str, n: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---- widgets ---------------------------------------------------------------------------


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


class MsgRow(Static):
    def __init__(self, msg_id: str):
        super().__init__("")
        self.msg_id = msg_id


class MessageView(VerticalScroll, can_focus=True):
    """The message list. Focused = selection mode."""

    BINDINGS = [
        Binding("up,k", "app.select_move(-1)", "prev", show=False),
        Binding("down,j", "app.select_move(1)", "next", show=False),
        Binding("r,enter", "app.start_reply", "reply", show=False),
        Binding("t", f"app.toggle_reaction('{THUMBS}')", "👍", show=False),
        Binding("h", f"app.toggle_reaction('{HEART}')", "❤️", show=False),
        Binding("escape", "app.end_select", "back", show=False),
    ]

    def on_blur(self, _: events.Blur) -> None:
        self.app.message_view_blurred()


class ComposeInput(Input):
    BINDINGS = [
        Binding("up", "app.select_last", "select message", show=False),
        Binding("escape", "app.compose_escape", "back", show=False),
    ]


# ---- app ---------------------------------------------------------------------------------


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
    #log { background: ansi_default; padding: 0 1; height: 1fr; }
    #log > MsgRow { height: auto; background: ansi_default; }
    #log > .note { color: ansi_bright_black; }

    #typing { height: 1; margin: 0 1; color: ansi_bright_black; text-style: italic; }
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
        self._rows: list[MsgRow] = []
        self._nick_width = 8
        self._selected: str | None = None  # message id in selection mode
        self._reply_to: Message | None = None
        self._polling = False
        self._last_poll = 0.0
        self._next_recover = 0.0
        self._recover_backoff = 15
        self._state = "starting browser…"
        # live stream
        self._last_frame = 0.0
        self._pending_groups: set[str] = set()
        self._stream_timer = None
        self._typing: dict[str, dict[str, float]] = {}  # group -> user -> expiry (monotonic)
        self._my_typing_sent = 0.0
        self._my_typing_timer = None
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
                yield MessageView(id="log")
                yield Static("", id="typing")
                with Horizontal(id="prompt-row"):
                    yield Static("›", id="prompt")
                    yield ComposeInput(placeholder="select a chat with ↑↓ and enter", id="compose", disabled=True)
        with Horizontal(id="statusbar"):
            yield Static("", id="status-left")
            yield Static("", id="status-right")

    def on_mount(self) -> None:
        self.query_one("#sidebar").border_title = "chats"
        self.query_one("#chat").border_title = "gchat"
        if self._driver is not None:
            self._driver.write("\x1b[22;0t")  # push the current title so we can restore it
        self._write_title("Google Chat")
        self.set_interval(1.0, self._tick)
        self.run_worker(self._show_note("connecting to Google Chat…"))
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
        if self._selected is not None:
            right = Text(f"↑↓ move  r reply  t {THUMBS}  h {HEART}  esc back")
        elif self._reply_to is not None:
            right = Text("enter send reply  esc cancel")
        else:
            right = Text("^q quit  esc chats  ↑ select message  ^r refresh")
        self.query_one("#status-left", Static).update(left)
        self.query_one("#sidebar").border_subtitle = f"{n} unread" if n else ""
        self.query_one("#status-right", Static).update(right)

    def _tick(self) -> None:
        """Once a second: tab-title flashing and typing-indicator expiry."""
        if self._flash_text:
            self._flash_phase = not self._flash_phase
            self.update_term_title()
        self._render_typing()

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
            if self._on_stream_frame not in self.session.stream_listeners:
                self.session.stream_listeners.append(self._on_stream_frame)
            if self.client is None:
                await self.session.start()
                self.client = ChatClient(self.session)
            await self.refresh_conversations(initial=True)
        except AuthError as e:
            await self._show_note(f"login failed: {e}", style="red")
            self.update_status("not connected")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("connect failed")
            await self._show_note(f"error: {e}", style="red")
            self.update_status("not connected")
            return
        await self._show_note("pick a chat on the left: ↑↓ to move, enter to open")
        self.update_status("connected")
        self.query_one("#convs", ListView).focus()
        self.set_interval(POLL_FAST, self._maybe_poll)

    def _stream_healthy(self) -> bool:
        return time.monotonic() - self._last_frame < STREAM_FRESH

    async def _maybe_poll(self) -> None:
        """Fallback polling: fast when the live stream is quiet, slow when it's working."""
        interval = POLL_SLOW if self._stream_healthy() else POLL_FAST
        if time.monotonic() - self._last_poll >= interval - 0.5:
            await self.poll()

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

    async def poll(self, force_reload: set[str] | None = None) -> None:
        """Refresh the conversation list; reload the open conversation if it changed
        (or if it's in force_reload, e.g. a reaction arrived over the stream)."""
        if self._polling or self.client is None or time.monotonic() < self._next_recover:
            if force_reload:
                self._pending_groups |= force_reload
                self._schedule_stream_refresh()
            return
        self._polling = True
        self._last_poll = time.monotonic()
        try:
            changed = await self.refresh_conversations()
            if self._state != "connected":
                self.update_status("connected")
            reloaded = False
            for c in changed:
                if self.current and c.key == self.current.key:
                    # only mark read if the user can actually see it
                    msgs = await self.load_messages(mark_read=self._term_focused)
                    reloaded = True
                    if msgs and msgs[-1].creator_id != self.client.me:
                        self._clear_typing(c.key, msgs[-1].creator_id)
                        self.alert(msgs[-1].creator_name or self.client.display_name(c))
                elif c.unread:
                    name = self.client.display_name(c)
                    self.notify(f"new message in {name}", timeout=6, markup=False)
                    self.alert(name)
            if force_reload and not reloaded and self.current and self.current.key in force_reload:
                await self.load_messages(mark_read=self._term_focused)
        except AuthError as e:
            log.warning("auth error during poll: %s", e)
            await self._recover()
        except Exception as e:  # noqa: BLE001
            log.warning("poll failed: %s", e)
            self.update_status(f"⚠ {type(e).__name__}, retrying")
        finally:
            self._polling = False

    # ---- live stream -------------------------------------------------------------
    def _on_stream_frame(self, url: str, frame: str) -> None:
        self._last_frame = time.monotonic()
        for ev in parse_frame(frame):
            if EVENT_TYPING in ev.types and ev.group and ev.typing_user:
                if self.client is None or ev.typing_user != self.client.me:
                    users = self._typing.setdefault(ev.group, {})
                    if ev.typing_state == TYPING:
                        users[ev.typing_user] = time.monotonic() + TYPING_TTL
                    else:
                        users.pop(ev.typing_user, None)
                    self._render_typing()
                continue
            if ev.group:
                self._pending_groups.add(ev.group)
        if self._pending_groups:
            self._schedule_stream_refresh()

    def _schedule_stream_refresh(self) -> None:
        # Events come in bursts (message + read state + badge); coalesce them.
        if self._stream_timer is None:
            self._stream_timer = self.set_timer(0.4, self._flush_stream_refresh)

    def _flush_stream_refresh(self) -> None:
        self._stream_timer = None
        groups, self._pending_groups = self._pending_groups, set()
        if groups:
            self.run_worker(self.poll(force_reload=groups), group="stream")

    # ---- typing indicators ---------------------------------------------------------
    def _clear_typing(self, group: str, user: str) -> None:
        self._typing.get(group, {}).pop(user, None)
        self._render_typing()

    def _render_typing(self) -> None:
        now = time.monotonic()
        for users in self._typing.values():
            for u in [u for u, exp in users.items() if exp < now]:
                del users[u]
        label = ""
        if self.current is not None and self.client is not None:
            users = list(self._typing.get(self.current.key, {}))
            names = [self.client.first_names.get(u) or self.client.names.get(u) or "someone" for u in users]
            if len(names) == 1:
                label = f"{names[0]} is typing…"
            elif len(names) == 2:
                label = f"{names[0]} and {names[1]} are typing…"
            elif names:
                label = "several people are typing…"
        try:
            self.query_one("#typing", Static).update(label)
        except Exception:  # noqa: BLE001 - not mounted yet / shutting down
            pass

    @on(Input.Changed, "#compose")
    def _on_compose_changed(self, ev: Input.Changed) -> None:
        if self.current is None or self.client is None:
            return
        if ev.value.strip():
            now = time.monotonic()
            if now - self._my_typing_sent > TYPING_RESEND:
                self._my_typing_sent = now
                self._send_typing(self.current, True)
            if self._my_typing_timer is not None:
                self._my_typing_timer.stop()
            self._my_typing_timer = self.set_timer(TYPING_IDLE, self._typing_idle)
        else:
            self._stop_my_typing()

    def _typing_idle(self) -> None:
        self._my_typing_timer = None
        self._stop_my_typing()

    def _stop_my_typing(self) -> None:
        if self._my_typing_timer is not None:
            self._my_typing_timer.stop()
            self._my_typing_timer = None
        if self._my_typing_sent and self.current is not None:
            self._my_typing_sent = 0.0
            self._send_typing(self.current, False)

    def _send_typing(self, conv: Conversation, typing: bool) -> None:
        async def go() -> None:
            try:
                await self.client.set_typing(conv, typing)
            except Exception as e:  # noqa: BLE001 - typing is best-effort
                log.info("set_typing failed: %s", e)

        self.run_worker(go(), group="typing")

    # ---- conversations ---------------------------------------------------------
    @on(ListView.Selected, "#convs")
    def open_selected(self, ev: ListView.Selected) -> None:
        if not isinstance(ev.item, ConvItem):
            return
        self._stop_my_typing()
        self._selected = None
        self._reply_to = None
        self.current = ev.item.conv
        name = self.client.display_name(self.current)
        chat = self.query_one("#chat")
        chat.border_title = name
        n = len(self.current.member_ids)
        chat.border_subtitle = "dm" if self.current.is_dm and n <= 2 else (f"{n} people" if n else "space")
        self._reset_compose()
        self.query_one("#compose", Input).focus()
        self._msgs = []
        self._render_typing()
        self.update_status()
        self.open_conversation()

    @work(exclusive=True, group="messages")
    async def open_conversation(self) -> None:
        await self._show_note("loading…")
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
        await self._render_messages()
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
    async def _show_note(self, text: str, style: str = "bright_black") -> None:
        view = self.query_one("#log", MessageView)
        await view.remove_children()
        self._rows = []
        await view.mount(Static(Text(text, style=style), classes="note"))

    def _nick(self, m: Message) -> Text:
        if m.creator_id == self.client.me:
            name = self.client.first_names.get(m.creator_id) or "me"
            return Text(name, style="bold")
        name = self.client.first_names.get(m.creator_id) or (m.creator_name.split() or ["?"])[0]
        return Text(name, style=f"bold {nick_color(m.creator_id)}")

    def _line(self, time_: str, nick: Text, body: Text, selected: bool = False) -> Table:
        """One irssi-style row: `HH:MM  nick │ message`. The message wraps in its own
        column and the divider repeats on every wrapped line."""
        w = self._nick_width
        nick = nick.copy()
        if len(nick.plain) > w:
            nick.truncate(w, overflow="ellipsis")
        nick.align("right", w)
        prefix = Text()
        if selected:
            prefix.append((time_ or "  ▸").ljust(5), style="bold reverse blue")
        else:
            prefix.append(time_.ljust(5), style="bright_black")
        prefix.append(" ")
        prefix.append_text(nick)
        g = Table(
            box=DIVIDER_SELECTED if selected else DIVIDER,
            show_header=False, show_edge=False, padding=(0, 1), expand=True,
            border_style="blue" if selected else "bright_black",
        )
        g.add_column(width=6 + w, no_wrap=True)
        g.add_column(ratio=1, overflow="fold")
        g.add_row(prefix, body)
        return g

    def _message_renderable(self, i: int, roots: dict[str, Message]) -> Group:
        msgs = self._msgs
        m = msgs[i]
        prev = msgs[i - 1] if i > 0 else None
        parts = []
        if prev is None or prev.time.date() != m.time.date():
            parts.append(Rule(day_label(m.time.date()), style="bright_black", characters="─"))
            prev = None
        same_block = (
            prev is not None
            and prev.creator_id == m.creator_id
            and m.time - prev.time <= timedelta(minutes=5)
        )
        body = Text()
        if m.is_reply:
            root = roots.get(m.topic_id)
            body.append("↳ ", style="bright_black")
            if root is not None:
                who = "you" if root.creator_id == self.client.me else self._nick(root).plain
                body.append(f"{who}: {snippet(root.text or '[attachment]')}\n", style="italic bright_black")
        body.append(m.text or "")
        for label in m.attachments:
            if body.plain.strip() and not body.plain.endswith("\n"):
                body.append(" ")
            body.append(f"[{label}]", style="italic cyan")
        if not m.text and not m.attachments:
            body.append("[unsupported message]", style="italic bright_black")
        if m.reactions:
            body.append("\n")
            for n, (emoji, (count, mine)) in enumerate(m.reactions.items()):
                if n:
                    body.append("  ")
                body.append(f"{emoji} {count}", style="bold blue" if mine else "bright_black")
        selected = m.id == self._selected
        time_ = m.time.strftime("%H:%M")
        if same_block and time_ == prev.time.strftime("%H:%M") and not selected:
            time_ = ""
        parts.append(self._line(time_, Text("") if same_block else self._nick(m), body, selected))
        return Group(*parts)

    async def _render_messages(self) -> None:
        view = self.query_one("#log", MessageView)
        msgs = self._msgs
        if not msgs:
            await self._show_note("no messages yet")
            return
        longest = max(len(self._nick(m).plain) for m in msgs)
        self._nick_width = max(4, min(MAX_NICK_WIDTH, longest))
        if self._selected and self._selected not in {m.id for m in msgs}:
            self._selected = None
        roots = {m.topic_id: m for m in msgs if not m.is_reply}
        at_bottom = view.scroll_y >= view.max_scroll_y - 1
        old_ids = [r.msg_id for r in self._rows]
        new_ids = [m.id for m in msgs]
        if old_ids and new_ids[: len(old_ids)] == old_ids:
            # Same history, maybe with new messages appended: update rows in place.
            new_rows = [MsgRow(mid) for mid in new_ids[len(old_ids):]]
            self._rows += new_rows
            if new_rows:
                await view.mount_all(new_rows)
        else:
            await view.remove_children()
            self._rows = [MsgRow(mid) for mid in new_ids]
            await view.mount_all(self._rows)
            at_bottom = True
        for i, row in enumerate(self._rows):
            row.update(self._message_renderable(i, roots))
        if self._selected:
            self._scroll_to_selected()
        elif at_bottom:
            self.call_after_refresh(view.scroll_end, animate=False)

    def _rerender_rows(self) -> None:
        roots = {m.topic_id: m for m in self._msgs if not m.is_reply}
        for i, row in enumerate(self._rows):
            row.update(self._message_renderable(i, roots))

    def _scroll_to_selected(self) -> None:
        row = next((r for r in self._rows if r.msg_id == self._selected), None)
        if row is not None:
            self.call_after_refresh(self.query_one("#log", MessageView).scroll_to_widget, row, animate=False)

    # ---- selection mode --------------------------------------------------------------
    def action_select_last(self) -> None:
        inp = self.query_one("#compose", Input)
        if inp.value or not self._msgs:
            return
        self._selected = self._msgs[-1].id
        self.query_one("#log", MessageView).focus()
        self._rerender_rows()
        self._scroll_to_selected()
        self.update_status()

    def action_select_move(self, delta: int) -> None:
        ids = [m.id for m in self._msgs]
        if not ids:
            return
        idx = ids.index(self._selected) if self._selected in ids else len(ids) - 1
        idx = max(0, min(len(ids) - 1, idx + delta))
        self._selected = ids[idx]
        self._rerender_rows()
        self._scroll_to_selected()
        self.update_status()

    def action_end_select(self) -> None:
        self.query_one("#compose", Input).focus()  # blur handler clears the selection

    def message_view_blurred(self) -> None:
        if self._selected is not None:
            self._selected = None
            self._rerender_rows()
            self.update_status()

    def _selected_message(self) -> Message | None:
        return next((m for m in self._msgs if m.id == self._selected), None)

    def action_start_reply(self) -> None:
        m = self._selected_message()
        if m is None or self.current is None:
            return
        self._reply_to = m
        who = "yourself" if m.creator_id == self.client.me else self._nick(m).plain
        inp = self.query_one("#compose", Input)
        inp.placeholder = f"reply to {who}: {snippet(m.text or '[attachment]', 50)}"
        self.query_one("#prompt", Static).update("↳")
        inp.focus()
        self.update_status()

    def action_compose_escape(self) -> None:
        if self._reply_to is not None:
            self._reply_to = None
            self._reset_compose()
            self.update_status()
        else:
            self.action_focus_list()

    def _reset_compose(self) -> None:
        inp = self.query_one("#compose", Input)
        inp.disabled = self.current is None
        if self.current is not None:
            inp.placeholder = f"message {self.client.display_name(self.current)}"
        self.query_one("#prompt", Static).update("›")

    def action_toggle_reaction(self, emoji: str) -> None:
        m = self._selected_message()
        conv = self.current
        if m is None or conv is None:
            return
        key = next((k for k in m.reactions if same_emoji(k, emoji)), emoji)
        count, mine = m.reactions.get(key, (0, False))
        add = not mine
        # optimistic update; the stream event / reload brings the server's truth
        if add:
            m.reactions[key] = (count + 1, True)
        elif count <= 1:
            m.reactions.pop(key, None)
        else:
            m.reactions[key] = (count - 1, False)
        self._rerender_rows()

        async def go() -> None:
            try:
                await self.client.react(conv, m, emoji, add)
            except Exception as e:  # noqa: BLE001
                log.exception("react failed")
                self.notify(f"reaction failed: {e}", severity="error", markup=False)
            if self.current and self.current.key == conv.key:
                await self.load_messages()

        self.run_worker(go(), group="react")

    # ---- sending -----------------------------------------------------------------
    @on(Input.Submitted, "#compose")
    def submit(self, ev: Input.Submitted) -> None:
        text = ev.value.strip()
        if not text or self.current is None:
            return
        ev.input.value = ""
        self._stop_my_typing()
        reply_to, self._reply_to = self._reply_to, None
        self._reset_compose()
        self.update_status()
        self.send_message(self.current, text, reply_to)

    @work(group="send")
    async def send_message(self, conv: Conversation, text: str, reply_to: Message | None = None) -> None:
        try:
            if reply_to is not None:
                await self.client.reply(conv, reply_to, text)
            else:
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
        view = self.query_one("#log", MessageView)
        (view.scroll_page_up if direction < 0 else view.scroll_page_down)()

    async def on_unmount(self) -> None:
        if self._driver is not None:
            self._driver.write("\x1b[23;0t")  # pop: restore the title from before we started
        try:
            await self.session.close()
        except Exception:  # noqa: BLE001
            pass
