"""Decode Google Chat's live event stream.

The web client keeps a WebChannel open to chat.google.com/punctual/.../multi-watch/channel.
Each frame is JSON; somewhere inside it are base64 strings that are binary protobuf
`Event` messages (see purple-googlechat's googlechat.proto). We decode only the few
fields we need, by field number, with a tiny wire-format reader (no protobuf dependency):

  Event:          1 group_id (GroupId), 3 type, 4 body (EventBody), 8 bodies (repeated)
  EventBody:      12 event_type, 26 typing_state_changed_event
  TypingStateChangedEvent: 1 state (1 TYPING, 2 STOPPED), 2 user_id (UserId{1 id}),
                           3 context (TypingContext{1 group_id})
  GroupId:        1 space_id (SpaceId{1 id}), 3 dm_id (DmId{1 id})

For everything other than typing we only need *which conversation* changed; the app then
re-fetches it through the normal API.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

EVENT_TYPING = 29
TYPING, STOPPED = 1, 2

_B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


@dataclass
class StreamEvent:
    group: str | None  # "dm/<id>" / "space/<id>", or None for account-level events
    types: list[int]  # EventType of each body
    typing_user: str | None = None
    typing_state: int | None = None


# ---- protobuf wire format ---------------------------------------------------------


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        byte = b[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def pb(b: bytes) -> dict[int, list]:
    """Parse one protobuf message into {field_number: [values]} (ints or raw bytes)."""
    out: dict[int, list] = {}
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        num, wire = key >> 3, key & 7
        if num == 0:
            raise ValueError("field 0")
        if wire == 0:
            v, i = _varint(b, i)
        elif wire == 2:
            n, i = _varint(b, i)
            if i + n > len(b):
                raise ValueError("truncated field")
            v, i = b[i : i + n], i + n
        elif wire == 1:
            v, i = b[i : i + 8], i + 8
        elif wire == 5:
            v, i = b[i : i + 4], i + 4
        else:
            raise ValueError(f"wire type {wire}")
        out.setdefault(num, []).append(v)
    return out


def _first(msg: dict, num: int, default=None):
    vals = msg.get(num)
    return vals[0] if vals else default


def _str(msg: dict, num: int) -> str | None:
    v = _first(msg, num)
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else None


def _group_key(raw: bytes | None) -> str | None:
    if not isinstance(raw, bytes):
        return None
    g = pb(raw)
    if space := _first(g, 1):
        if sid := _str(pb(space), 1):
            return f"space/{sid}"
    if dm := _first(g, 3):
        if did := _str(pb(dm), 1):
            return f"dm/{did}"
    return None


def parse_event(raw: bytes) -> StreamEvent:
    ev = pb(raw)
    group = _group_key(_first(ev, 1))
    types: list[int] = []
    out = StreamEvent(group=group, types=types)
    t = _first(ev, 3)
    if isinstance(t, int):
        types.append(t)
    for body_raw in ev.get(4, []) + ev.get(8, []):
        if not isinstance(body_raw, bytes):
            continue
        body = pb(body_raw)
        et = _first(body, 12)
        if isinstance(et, int):
            types.append(et)
        typing = _first(body, 26)
        if isinstance(typing, bytes):
            ts = pb(typing)
            out.typing_state = _first(ts, 1)
            user = _first(ts, 2)
            out.typing_user = _str(pb(user), 1) if isinstance(user, bytes) else None
            ctx = _first(ts, 3)
            if isinstance(ctx, bytes) and (g := _group_key(_first(pb(ctx), 1))):
                out.group = g
            if EVENT_TYPING not in types:
                types.append(EVENT_TYPING)
    return out


# ---- WebChannel frames --------------------------------------------------------------


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for x in node:
            yield from _strings(x)


def parse_frame(frame: str) -> list[StreamEvent]:
    """A WebChannel frame (JSON) -> the Chat events inside it. Never raises."""
    try:
        data = json.loads(frame)
    except ValueError:
        return []
    events = []
    for s in _strings(data):
        if len(s) < 16 or not _B64.match(s):
            continue
        try:
            raw = base64.b64decode(s, validate=True)
            ev = parse_event(raw)
        except (ValueError, binascii.Error):
            continue
        if ev.group or ev.types:
            events.append(ev)
    return events
