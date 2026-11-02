"""Platform-neutral intermediate representation.

The Android parser produces these objects; the iOS writer consumes them.
Keeping both sides decoupled through this model is what will later let us
add iOS -> Android, or a different backend, without touching the parsers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MsgKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"          # music / audio files
    VOICE = "voice"          # push-to-talk voice notes
    DOCUMENT = "document"
    GIF = "gif"
    STICKER = "sticker"
    CONTACT = "contact"
    LOCATION = "location"
    SYSTEM = "system"        # group events, security notices, ...
    REVOKED = "revoked"      # "This message was deleted"
    UNKNOWN = "unknown"      # polls, payments, view-once, ... (imported as text placeholder)


@dataclass
class Media:
    """A media attachment. `android_path` is relative to the WhatsApp/ folder on Android."""

    android_path: str | None
    mime_type: str | None = None
    size: int | None = None
    duration_s: int | None = None
    width: int | None = None
    height: int | None = None
    file_name: str | None = None      # documents keep their original name
    caption: str | None = None
    file_hash: str | None = None      # base64 SHA-256 of the file: proves which file belongs to the message

    @property
    def basename(self) -> str | None:
        return self.android_path.rsplit("/", 1)[-1] if self.android_path else None


@dataclass
class Location:
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None
    live: bool = False


@dataclass
class Quote:
    """The message a reply points at."""

    key_id: str
    from_me: bool = False
    sender_jid: str | None = None     # None when from_me, or in a 1:1 chat where it is the chat itself
    text: str | None = None           # quoted text, or a short label for media ("Photo")


@dataclass
class Reaction:
    key_id: str
    emoji: str
    timestamp_ms: int
    from_me: bool = False
    sender_jid: str | None = None


@dataclass
class Message:
    key_id: str                       # WhatsApp stanza/message id (stable across platforms)
    from_me: bool
    timestamp_ms: int                 # Unix epoch milliseconds, UTC
    kind: MsgKind = MsgKind.TEXT
    text: str | None = None
    sender_jid: str | None = None     # for group chats; None for 1:1 or from_me
    media: Media | None = None
    location: Location | None = None
    vcard: str | None = None          # raw vCard text for CONTACT messages
    quoted_key_id: str | None = None
    quote: Quote | None = None
    reactions: list[Reaction] = field(default_factory=list)
    revoked_key_id: str | None = None # REVOKED only: Android's `message_revoked.revoked_key_id`
    starred: bool = False
    android_status: int | None = None
    android_type: int | None = None   # raw message_type, kept for debugging


@dataclass
class Participant:
    jid: str
    name: str | None = None
    is_admin: bool = False
    active: bool = True


@dataclass
class Chat:
    jid: str                          # e.g. 61400000000@s.whatsapp.net or 123-456@g.us
    lid: str | None = None            # WhatsApp "LID" alias (<n>@lid) for this user, when known
    name: str | None = None           # contact name or group subject
    is_group: bool = False
    created_ms: int | None = None
    archived: bool = False
    participants: list[Participant] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)

    @property
    def phone(self) -> str | None:
        """Bare phone number for user JIDs, else None."""
        if self.jid.endswith("@s.whatsapp.net"):
            return self.jid.split("@", 1)[0]
        return None


@dataclass
class Archive:
    """Everything we know about one Android WhatsApp account."""

    owner_jid: str | None
    chats: list[Chat]
    contacts: dict[str, str] = field(default_factory=dict)   # jid -> display name
    phone_to_lid: dict[str, str] = field(default_factory=dict)   # phone JID -> `<n>@lid`, when Android knows it

    def stats(self) -> dict[str, int]:
        msgs = sum(len(c.messages) for c in self.chats)
        media = sum(1 for c in self.chats for m in c.messages if m.media)
        return {
            "chats": len(self.chats),
            "groups": sum(1 for c in self.chats if c.is_group),
            "messages": msgs,
            "media": media,
        }
