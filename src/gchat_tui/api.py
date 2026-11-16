"""Google Chat's private web API, decoded with protobuf field numbers.

Requests/responses are "pblite": a protobuf message is a JSON array where field N sits
at index N-1. Field numbers come from purple-googlechat's googlechat.proto
(https://github.com/EionRobb/purple-googlechat). Field numbers are a wire contract
shared with Google's older clients, so they are far more stable than the web UI's DOM.

All knowledge of Google's format lives in this file. If Chat breaks, start here.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import datetime

from .session import Session


def f(msg, n: int, default=None):
    """Field n (1-based) of a pblite message, or default if absent."""
    if isinstance(msg, list) and len(msg) >= n and msg[n - 1] is not None:
        return msg[n - 1]
    return default


def i64(v) -> int:
    """int64s arrive as JSON strings or numbers."""
    return int(v) if v not in (None, "") else 0


# ---- models -------------------------------------------------------------------


@dataclass
class Conversation:
    key: str  # "dm/<id>" or "space/<id>"
    gid: list  # pblite GroupId, passed back to the API as-is
    name: str | None  # explicit room name (spaces); DMs are named from members
    member_ids: list[str] = field(default_factory=list)
    sort_ts: int = 0
    last_read: int = 0
    starred: bool = False

    @property
    def is_dm(self) -> bool:
        return self.key.startswith("dm/")

    @property
    def raw_id(self) -> str:
        return self.key.split("/", 1)[1]

    @property
    def unread(self) -> bool:
        return self.sort_ts > self.last_read


@dataclass
class Message:
    id: str
    topic_id: str
    creator_id: str
    creator_name: str
    time_us: int
    text: str
    is_reply: bool = False  # a thread reply (not the topic's first message)
    attachments: list[str] = field(default_factory=list)  # labels like "image"

    @property
    def time(self) -> datetime:
        return datetime.fromtimestamp(self.time_us / 1e6)


def _gid_key(gid) -> str | None:
    space = f(f(gid, 1), 1)
    if space:
        return f"space/{space}"
    dm = f(f(gid, 3), 1)
    if dm:
        return f"dm/{dm}"
    return None


def _parse_world_item(it) -> Conversation | None:
    gid = f(it, 1)  # WorldItemLite.group_id
    key = _gid_key(gid)
    if not key:
        return None
    rs = f(it, 4)  # read_state: GroupReadState
    dm = f(it, 6)  # dm_members: DmMembers
    # DMs list members in dm_members; unnamed group chats in name_users (field 20)
    member_list = f(dm, 1) or f(f(it, 20), 1, [])
    members = [f(u, 1) for u in member_list]
    return Conversation(
        key=key,
        gid=gid,
        name=f(it, 5),  # room_name
        member_ids=[m for m in members if m],
        sort_ts=i64(f(it, 3)),  # sort_timestamp
        last_read=i64(f(rs, 2)),  # GroupReadState.last_read_time
        starred=bool(f(rs, 5)),
    )


DRIVE_TYPES = {2, 3, 4, 5, 18}  # AnnotationType: DRIVE_FILE/DOC/SHEET/SLIDE/FORM


def _attachment_labels(m) -> list[str]:
    """Describe uploads / Drive files / media attached to a Message."""
    labels = []
    for a in f(m, 11, []):  # Message.annotations
        kind = f(a, 1)  # Annotation.type
        if kind == 13:  # UPLOAD_METADATA
            up = f(a, 10)
            ctype = f(up, 4, "")
            if ctype.startswith("image/"):
                labels.append("image")
            elif ctype.startswith("video/"):
                labels.append("video")
            else:
                labels.append(f"file: {f(up, 3, 'attachment')}")
        elif kind in DRIVE_TYPES:
            labels.append(f"drive: {f(f(a, 4), 2, 'file')}")  # drive_metadata.title
        elif kind == 9:
            labels.append("image")
        elif kind == 7:
            labels.append("video")
    if not labels and f(m, 15):  # Message.attachments (cards, etc.)
        labels.append("attachment")
    return labels


def _parse_message(m, is_reply: bool) -> Message:
    creator = f(m, 2) or []  # User
    mid = f(m, 1)  # MessageId
    topic = f(f(f(mid, 1), 4), 2)  # parent_id.topic_id.topic_id
    return Message(
        id=f(mid, 2, ""),
        topic_id=topic or "",
        creator_id=f(f(creator, 1), 1, ""),
        creator_name=f(creator, 2, ""),
        time_us=i64(f(m, 3)),  # create_time
        text=f(m, 10, ""),  # text_body
        is_reply=is_reply,
        attachments=_attachment_labels(m),
    )


# ---- client -------------------------------------------------------------------


class ChatClient:
    def __init__(self, session: Session):
        self.s = session
        self.me: str | None = None
        self.names: dict[str, str] = {}  # user id -> display name
        self.first_names: dict[str, str] = {}

    async def list_conversations(self, limit: int = 50) -> list[Conversation]:
        # PaginatedWorldRequest: 1 header, 2 sections[WorldSectionRequest{1 page_size}],
        # 4 fetch_options (5 = user profiles for naming)
        body = [self.s.request_header, [[limit]], None, [5]]
        resp = await self.s.call("paginated_world", body)
        items = f(resp, 4, [])  # PaginatedWorldResponse.world_items
        convs = [c for c in map(_parse_world_item, items) if c]
        if self.me is None and items:
            # GroupReadState.id (GroupReadStateId).user_id is us
            self.me = f(f(f(f(items[0], 4), 1), 1), 1)
        await self._resolve_names({m for c in convs for m in c.member_ids})
        return sorted(convs, key=lambda c: c.sort_ts, reverse=True)

    async def _resolve_names(self, ids: set[str]) -> None:
        missing = [i for i in ids if i not in self.names]
        if not missing:
            return
        # GetMembersRequest.member_ids: MemberId{1 user_id: UserId{1 id}}
        resp = await self.s.call("get_members", self.s.req({1: [[[i]] for i in missing]}))
        for member in f(resp, 1, []):  # GetMembersResponse.members
            user = f(member, 1)
            uid = f(f(user, 1), 1)
            if uid:
                self.names[uid] = f(user, 2) or uid
                self.first_names[uid] = f(user, 5) or self.names[uid]

    def display_name(self, c: Conversation) -> str:
        if c.name:
            return c.name
        others = [m for m in c.member_ids if m != self.me]
        if not others:
            return f"{self.names.get(self.me, 'Me')} (you)"
        if len(others) == 1:
            return self.names.get(others[0], others[0])
        return ", ".join(self.first_names.get(m, m) for m in others)

    async def list_messages(self, c: Conversation, topics: int = 40) -> list[Message]:
        # ListTopicsRequest: 2 page_size_for_topics, 5 fetch_options(USER),
        # 6 page_size_for_unread_replies, 7 page_size_for_read_replies, 8 group_id
        body = self.s.req({2: topics, 5: [1], 6: 1000, 7: 30, 8: c.gid})
        resp = await self.s.call("list_topics", body, space_id=c.raw_id)
        out: list[Message] = []
        for t in f(resp, 1, []):  # ListTopicsResponse.topics
            for n, m in enumerate(f(t, 7, [])):  # Topic.replies
                msg = _parse_message(m, is_reply=n > 0)
                out.append(msg)
                if msg.creator_name:
                    self.names.setdefault(msg.creator_id, msg.creator_name)
        out.sort(key=lambda m: m.time_us)
        return out

    async def send(self, c: Conversation, text: str) -> None:
        local_id = "gct-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        # CreateTopicRequest: 2 text_body, 4 local_id, 5 group_id, 8 history_v2
        body = self.s.req({2: text, 4: local_id, 5: c.gid, 8: True})
        await self.s.call("create_topic", body, space_id=c.raw_id)

    async def mark_read(self, c: Conversation) -> None:
        # MarkGroupReadstateRequest: 1 group_id, 2 last_read_time
        body = self.s.req({1: c.gid, 2: c.sort_ts})
        await self.s.call("mark_group_readstate", body, space_id=c.raw_id)
        c.last_read = max(c.last_read, c.sort_ts)
