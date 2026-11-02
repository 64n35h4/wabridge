"""Write the intermediate model into WhatsApp-for-iOS `ChatStorage.sqlite`.

ChatStorage.sqlite is a Core Data store. We write to it with plain SQL (the approach
proven by mukulkadel/mwatoi) instead of through Core Data + the app's `.momd` model
(residentsummer/watoi), which keeps the tool cross-platform and independent of the IPA.

Rules for hand-written Core Data rows:
  * `Z_ENT` for an entity comes from `Z_PRIMARYKEY.Z_NAME` — never hard-code it.
  * `Z_OPT` starts at 1.
  * New `Z_PK` = `Z_PRIMARYKEY.Z_MAX + 1`, and `Z_MAX` must be bumped afterwards or
    WhatsApp will collide with our rows on its next save.
  * Dates are Cocoa epoch seconds (2001-01-01), stored as REAL.
  * Only columns that actually exist are written (introspected with PRAGMA), so minor
    model changes between WhatsApp versions don't break the import.

Field conventions, measured on rows WhatsApp itself wrote (iOS 27.0.1, 113,763 messages, Nov 2026):
  * ZWAMESSAGE.ZFROMJID = the session's ZCONTACTJID for incoming messages, NULL when from me; ZTOJID = the same
    for outgoing messages. So in a @lid session they are @lid (632 of 632 rows). Group senders go through ZGROUPMEMBER.
  * ZWAGROUPMEMBER.ZMEMBERJID: active members are @lid (3,393 of 3,393); phone JIDs are only ever inactive (782 of 782).
    People are written by LID when Android knows it, or a group that synced at login lists them twice. Reply and
    reaction senders use the LID too: the only form in @lid sessions; groups hold both.
  * ZFLAGS: 0x1000000 on text, 0x5000000 on files (image, video, audio, document, GIF, sticker).
  * ZGROUPEVENTTYPE is 2 on ordinary messages. ZSTARRED is 1 or NULL. ZSENTDATE is NULL except on revoked
    messages. ZLASTSESSION is set only on the message that is its session's ZLASTMESSAGE.
  * ZMEDIASECTIONID (the media gallery) is set only when the file exists, and not on stickers or voice.
  * ZWAMEDIAITEM.ZVCARDNAME is the file's base64 SHA-256, the same value as Android's file_hash.
  * Captions live in ZWAMEDIAITEM.ZTITLE. ZTEXT is NULL for media, except documents (file name).
  * ZMEDIALOCALPATH and ZXMPPTHUMBPATH are relative to `Message/` (`Media/<jid>/<x>/<y>/<uuid>.<ext>`,
    x and y being the uuid's first two characters), and the MIME type is in ZVCARDSTRING.
  * A media row with no file simply has ZMEDIALOCALPATH NULL: the bubble still appears.
  * ZWAMEDIAITEM.ZMETADATA is protobuf. Replies: 5 = quoted stanza id, 6 = quoted sender JID,
    19 = quoted message (WhatsApp's Message proto; 1 = text), 211 = 0. A text message gets a media
    item only to carry this.
  * ZWAMESSAGEINFO.ZRECEIPTINFO is protobuf. Reactions: 7 { 1 { 1 = reaction stanza id, 2 = sender JID,
    3 = emoji, 4 = time in ms, 5 = 1, 6 = 1 if from me, 7 = 1 } ... }. A blob holding only field 7 is valid.
  * Revoked messages (type 14) have a media item with a stanza id in ZTITLE and the session's ZCONTACTJID in
    ZVCARDNAME (515 of 515 rows). That the stanza id is Android's revoked_key_id is inferred (unverified).
  * ZWAMESSAGE.ZGROUPEVENTTYPE codes are not mapped yet, so group events are not written.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field

from ..model import Archive, Chat, Media, Message, MsgKind, Quote, Reaction

log = logging.getLogger(__name__)

COCOA_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

# MsgKind -> ZMESSAGETYPE
IOS_TYPE: dict[MsgKind, int] = {
    MsgKind.TEXT: 0,
    MsgKind.IMAGE: 1,
    MsgKind.VIDEO: 2,
    MsgKind.AUDIO: 3,
    MsgKind.VOICE: 3,
    MsgKind.CONTACT: 4,
    MsgKind.LOCATION: 5,
    MsgKind.SYSTEM: 6,
    MsgKind.DOCUMENT: 8,
    MsgKind.GIF: 11,
    MsgKind.REVOKED: 14,      # unverified
    MsgKind.STICKER: 15,
    MsgKind.UNKNOWN: 0,
}
MEDIA_KINDS = {MsgKind.IMAGE, MsgKind.VIDEO, MsgKind.AUDIO, MsgKind.VOICE, MsgKind.DOCUMENT,
               MsgKind.GIF, MsgKind.STICKER}

FLAGS_TEXT = 0x1000000
FLAGS_FILE = 0x5000000
ORIGIN_VOICE = 1
THUMB_LONG_EDGE = 72           # real .thumb files are 72 px on the long edge
DEFAULT_MIME: dict[MsgKind, str] = {
    MsgKind.IMAGE: "image/jpeg", MsgKind.VIDEO: "video/mp4", MsgKind.GIF: "video/mp4",
    MsgKind.VOICE: "audio/ogg; codecs=opus", MsgKind.AUDIO: "audio/mp4", MsgKind.STICKER: "image/webp",
    MsgKind.DOCUMENT: "application/octet-stream",
}
GALLERY_KINDS = {MsgKind.IMAGE, MsgKind.VIDEO, MsgKind.GIF, MsgKind.DOCUMENT}
EVENT_NONE = 2                 # ZGROUPEVENTTYPE of an ordinary message
MIN_SAMPLE = 20                # a convention needs this many rows behind it: one pending message proves nothing
SESSION_INDIVIDUAL = 0
SESSION_GROUP = 1


class ChatStorageError(Exception):
    pass


def to_cocoa(ms: int) -> float:
    return ms / 1000.0 - COCOA_EPOCH_OFFSET


def media_db_path(chat_jid: str, key_id: str, ext: str) -> str:
    """The value for ZMEDIALOCALPATH / ZXMPPTHUMBPATH: `Media/<jid>/<x>/<y>/<uuid><ext>`, relative to
    `Message/`. The uuid derives from the message so re-runs give the same path."""
    name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chat_jid}/{key_id}"))
    return f"Media/{chat_jid}/{name[0]}/{name[1]}/{name}{ext}"


def manifest_path(db_path: str) -> str:
    """Where a ZMEDIALOCALPATH value lives in the backup."""
    return f"Message/{db_path}"


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pb_int(number: int, value: int) -> bytes:
    return _pb_varint(number << 3) + _pb_varint(value)


def _pb_bytes(number: int, value: bytes) -> bytes:
    return _pb_varint(number << 3 | 2) + _pb_varint(len(value)) + value


def _pb_str(number: int, value: str) -> bytes:
    return _pb_bytes(number, value.encode("utf-8"))


def encode_reply(quote: Quote, sender_jid: str | None) -> bytes:
    """ZMETADATA of a reply. Quoted media is written as text: its full Message proto isn't in msgstore."""
    out = _pb_str(5, quote.key_id)
    if sender_jid:
        out += _pb_str(6, sender_jid)
    return out + _pb_bytes(19, _pb_str(1, quote.text or "Message")) + _pb_int(211, 0)


def encode_reactions(reactions: list[Reaction], phone_to_lid: dict[str, str] | None = None) -> bytes:
    """ZRECEIPTINFO holding only reactions."""
    items = b""
    for r in reactions:
        item = _pb_str(1, r.key_id)
        if r.sender_jid and not r.from_me:
            item += _pb_str(2, (phone_to_lid or {}).get(r.sender_jid, r.sender_jid))
        item += _pb_str(3, r.emoji) + _pb_int(4, r.timestamp_ms) + _pb_int(5, 1)
        if r.from_me:
            item += _pb_int(6, 1)
        items += _pb_bytes(1, item + _pb_int(7, 1))
    return _pb_bytes(7, items)


@dataclass
class MediaJob:
    local_file: str
    relative_path: str


@dataclass
class ImportReport:
    sessions_created: int = 0
    sessions_reused: int = 0
    messages_written: int = 0
    messages_skipped_duplicate: int = 0
    media_linked: int = 0
    media_missing: int = 0
    thumbnails: int = 0
    replies: int = 0
    replies_without_sender: int = 0
    reactions: int = 0
    revoked: int = 0
    system_skipped: int = 0
    media_jobs: list[MediaJob] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Writer:
    def __init__(self, db_path: str):
        if not os.path.isfile(db_path):
            raise ChatStorageError(f"{db_path} not found")
        self.path = db_path
        self.thumb_dir: str | None = None
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.columns: dict[str, list[str]] = {}
        for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            self.columns[name] = [r[1] for r in self.conn.execute(f'PRAGMA table_info("{name}")')]
        for required in ("Z_PRIMARYKEY", "ZWACHATSESSION", "ZWAMESSAGE"):
            if required not in self.columns:
                raise ChatStorageError(f"{db_path} lacks {required}; not a WhatsApp ChatStorage.sqlite")
        self.entities = {
            r["Z_NAME"]: {"ent": r["Z_ENT"], "max": r["Z_MAX"] or 0}
            for r in self.conn.execute("SELECT Z_ENT, Z_NAME, Z_MAX FROM Z_PRIMARYKEY")
        }
        row = self.conn.execute("SELECT MAX(ZSORT) FROM ZWAMESSAGE").fetchone()
        self._sort = int(row[0] or 0)
        self.conv = self._learn_conventions()

    # ------------------------------------------------------------------ conventions
    def _mode(self, sql: str, params=(), default=None, min_rows: int = 1):
        """Most common value of a column among rows WhatsApp itself wrote."""
        counted = sql.replace(" FROM ", ", COUNT(*) FROM ", 1)
        try:
            row = self.conn.execute(counted + " GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1", params).fetchone()
        except sqlite3.DatabaseError:
            return default
        return row[0] if row and row[0] is not None and row[1] >= min_rows else default

    def _learn_conventions(self) -> dict:
        """WhatsApp for iOS (2025+) stores protobuf blobs in ZPUSHNAME / ZLASTMESSAGETEXT and expects
        specific ZFLAGS bits. Rather than hard-code a moving target, copy what the real rows in this
        very database do; fall back to values observed in Sept-2026 builds."""
        c = {
            "msg_flags_in": self._mode("SELECT ZFLAGS FROM ZWAMESSAGE WHERE ZISFROMME=0 AND ZMESSAGETYPE=0",
                                       default=FLAGS_TEXT, min_rows=MIN_SAMPLE),
            "msg_flags_out": self._mode("SELECT ZFLAGS FROM ZWAMESSAGE WHERE ZISFROMME=1 AND ZMESSAGETYPE=0",
                                        default=FLAGS_TEXT, min_rows=MIN_SAMPLE),
            "msg_flags_file": self._mode("SELECT ZFLAGS FROM ZWAMESSAGE WHERE ZMESSAGETYPE IN (1, 2, 3, 8, 11, 15)",
                                         default=FLAGS_FILE, min_rows=MIN_SAMPLE),
            "msg_group_event": self._mode("SELECT ZGROUPEVENTTYPE FROM ZWAMESSAGE WHERE ZMESSAGETYPE=0",
                                          default=EVENT_NONE, min_rows=MIN_SAMPLE),
            "msg_dataver": self._mode("SELECT ZDATAITEMVERSION FROM ZWAMESSAGE", default=3),
            "msg_spot": self._mode("SELECT ZSPOTLIGHTSTATUS FROM ZWAMESSAGE", default=-32768),
            "msg_status_in": self._mode("SELECT ZMESSAGESTATUS FROM ZWAMESSAGE WHERE ZISFROMME=0 AND ZMESSAGETYPE=0",
                                        default=0, min_rows=MIN_SAMPLE),
            "msg_status_out": self._mode("SELECT ZMESSAGESTATUS FROM ZWAMESSAGE WHERE ZISFROMME=1", default=8,
                                         min_rows=MIN_SAMPLE),
            "sess_flags_1to1": self._mode("SELECT ZFLAGS FROM ZWACHATSESSION WHERE ZSESSIONTYPE=0", default=272),
            "sess_flags_group": self._mode("SELECT ZFLAGS FROM ZWACHATSESSION WHERE ZSESSIONTYPE=1", default=256),
            "sess_spot_1to1": self._mode("SELECT ZSPOTLIGHTSTATUS FROM ZWACHATSESSION WHERE ZSESSIONTYPE=0",
                                         default=-5),
            "sess_spot_group": self._mode("SELECT ZSPOTLIGHTSTATUS FROM ZWACHATSESSION WHERE ZSESSIONTYPE=1",
                                          default=1),
        }
        # Is ZPUSHNAME a plain name or an opaque blob in this build? Sample one real row.
        sample = self.conn.execute(
            "SELECT ZPUSHNAME FROM ZWAMESSAGE WHERE ZPUSHNAME IS NOT NULL AND ZPUSHNAME<>'' LIMIT 1").fetchone()
        # No sample means nothing proves plain text is safe, and plain text there breaks the app.
        c["pushname_is_blob"] = not sample or _looks_like_base64_blob(sample[0])
        sample = self.conn.execute(
            "SELECT ZLASTMESSAGETEXT FROM ZWACHATSESSION WHERE ZLASTMESSAGETEXT IS NOT NULL LIMIT 1").fetchone()
        c["lasttext_is_blob"] = not sample or _looks_like_base64_blob(sample[0])
        # Modern builds do not use WAChatProperties at all
        c["use_chat_properties"] = bool(self.conn.execute(
            "SELECT COUNT(*) FROM ZWACHATSESSION WHERE ZPROPERTIES IS NOT NULL").fetchone()[0]) \
            if self.has_table("ZWACHATPROPERTIES") else False
        c["numeric_defaults"] = {t: self._never_null_numeric(t) for t in ("ZWAMEDIAITEM", "ZWAMESSAGE",
                                                                             "ZWACHATSESSION", "ZWAGROUPMEMBER")
                                 if self.has_table(t)}
        log.info("conventions learned from existing rows: %s", c)
        return c

    def _never_null_numeric(self, table: str) -> dict[str, int | float]:
        """Numeric columns that are non-null in every row WhatsApp wrote → Core Data treats them as
        non-optional scalars, and a NULL there crashes the app on load. Default them to 0."""
        info = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        n = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out: dict[str, int | float] = {}
        for _cid, name, ctype, *_ in info:
            if name.startswith("Z_") or not name.startswith("Z"):
                continue
            ct = (ctype or "").upper()
            if not any(k in ct for k in ("INT", "FLOAT", "REAL", "DOUBLE", "TIMESTAMP")):
                continue
            if "TIMESTAMP" in ct or "DATE" in name:
                continue                                    # dates may legitimately be NULL
            if n == 0:
                out[name] = 0.0 if any(k in ct for k in ("FLOAT", "REAL", "DOUBLE")) else 0
                continue
            nulls = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {name} IS NULL").fetchone()[0]
            if nulls == 0:
                out[name] = 0.0 if any(k in ct for k in ("FLOAT", "REAL", "DOUBLE")) else 0
        return out

    # ------------------------------------------------------------------ core helpers
    def has_table(self, t: str) -> bool:
        return t in self.columns

    def message_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]

    def _next_pk(self, entity: str) -> int:
        e = self.entities.get(entity)
        if not e:
            raise ChatStorageError(f"Entity {entity} not in Z_PRIMARYKEY (schema too new/old?)")
        e["max"] += 1
        return e["max"]

    def insert(self, table: str, entity: str, values: dict) -> int:
        cols = self.columns[table]
        pk = self._next_pk(entity)
        row = {"Z_PK": pk, "Z_ENT": self.entities[entity]["ent"], "Z_OPT": 1}
        for k, v in values.items():
            if k in cols:
                row[k] = v
        for k, default in getattr(self, "conv", {}).get("numeric_defaults", {}).get(table, {}).items():
            if row.get(k) is None:
                row[k] = default
        names = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO {table} ({names}) VALUES ({qs})", list(row.values()))
        return pk

    def update(self, table: str, pk: int, values: dict) -> None:
        cols = self.columns[table]
        vals = {k: v for k, v in values.items() if k in cols}
        if not vals:
            return
        sets = ", ".join(f"{k}=?" for k in vals)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE Z_PK=?", [*vals.values(), pk])

    def _flush_primary_keys(self) -> None:
        for name, e in self.entities.items():
            self.conn.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_NAME=?", (e["max"], name))

    def commit(self) -> None:
        self._flush_primary_keys()
        self.conn.commit()

    def finalize(self) -> None:
        """Commit, fold the WAL into the main file and close, leaving a single-file DB."""
        self.commit()
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.DatabaseError as e:   # pragma: no cover
            log.warning("checkpoint failed: %s", e)
        self.conn.close()

    # ------------------------------------------------------------------ lookups
    def find_session(self, *jids: str | None) -> sqlite3.Row | None:
        cands = [j for j in jids if j]
        if not cands:
            return None
        qs = ",".join("?" for _ in cands)
        return self.conn.execute(
            f"SELECT * FROM ZWACHATSESSION WHERE ZCONTACTJID IN ({qs}) OR ZCONTACTIDENTIFIER IN ({qs}) "
            "ORDER BY ZSESSIONTYPE LIMIT 1", cands + cands).fetchone()

    def existing_stanza_ids(self, session_pk: int) -> set[str]:
        return {
            r[0] for r in self.conn.execute(
                "SELECT ZSTANZAID FROM ZWAMESSAGE WHERE ZCHATSESSION=? AND ZSTANZAID IS NOT NULL", (session_pk,)
            )
        }

    def existing_members(self, session_pk: int) -> dict[str, int]:
        if not self.has_table("ZWAGROUPMEMBER"):
            return {}
        return {
            r["ZMEMBERJID"]: r["Z_PK"] for r in self.conn.execute(
                "SELECT Z_PK, ZMEMBERJID FROM ZWAGROUPMEMBER WHERE ZCHATSESSION=?", (session_pk,)
            )
        }

    # ------------------------------------------------------------------ import
    def import_archive(self, archive: Archive, *, media_root: str | None = None, include_media: bool = True,
                       thumb_dir: str | None = None) -> ImportReport:
        rep = ImportReport()
        self.thumb_dir = thumb_dir
        for chat in archive.chats:
            self._import_chat(chat, archive, rep, media_root, include_media)
        if rep.messages_written:
            self.renumber_sort()
        self.commit()
        return rep

    def _import_chat(self, chat: Chat, archive: Archive, rep: ImportReport,
                     media_root: str | None, include_media: bool) -> None:
        messages = [m for m in chat.messages if m.kind is not MsgKind.SYSTEM]
        # ZGROUPEVENTTYPE codes aren't mapped yet: see the module docstring
        rep.system_skipped += len(chat.messages) - len(messages)
        if not messages:                          # every real session has a last message
            return
        display = chat.name or chat.phone or chat.jid.split("@")[0]
        session = self.find_session(chat.jid, chat.lid)
        cv = self.conv
        if session:
            spk = session["Z_PK"]
            media_jid = session["ZCONTACTJID"]
            rep.sessions_reused += 1
        else:
            # Modern 1:1 sessions: ZCONTACTJID = LID (if known), ZCONTACTIDENTIFIER = phone JID.
            media_jid = (chat.lid or chat.jid) if not chat.is_group else chat.jid
            values = {
                "ZCONTACTJID": media_jid,
                "ZCONTACTIDENTIFIER": None if chat.is_group else chat.jid,
                "ZPARTNERNAME": display,
                "ZSESSIONTYPE": SESSION_GROUP if chat.is_group else SESSION_INDIVIDUAL,
                "ZMESSAGECOUNTER": 0,
                "ZUNREADCOUNT": 0,
                "ZARCHIVED": 1 if chat.archived else 0,
                "ZHIDDEN": 0,
                "ZREMOVED": 0,
                "ZCONTACTABID": 0,
                "ZIDENTITYVERIFICATIONEPOCH": 0,
                "ZIDENTITYVERIFICATIONSTATE": 0,
                "ZFLAGS": cv["sess_flags_group"] if chat.is_group else cv["sess_flags_1to1"],
                "ZSPOTLIGHTSTATUS": cv["sess_spot_group"] if chat.is_group else cv["sess_spot_1to1"],
                "ZLASTMESSAGEDATE": to_cocoa(messages[-1].timestamp_ms),
            }
            spk = self.insert("ZWACHATSESSION", "WAChatSession", values)
            rep.sessions_created += 1
            if chat.is_group and self.has_table("ZWAGROUPINFO") and "WAGroupInfo" in self.entities:
                gpk = self.insert("ZWAGROUPINFO", "WAGroupInfo", {
                    "ZCHATSESSION": spk,
                    "ZCREATIONDATE": to_cocoa(chat.created_ms) if chat.created_ms else None,
                    "ZGENERATION": 0,
                })
                self.update("ZWACHATSESSION", spk, {"ZGROUPINFO": gpk})
            if cv["use_chat_properties"] and "WAChatProperties" in self.entities:
                ppk = self.insert("ZWACHATPROPERTIES", "WAChatProperties", {"ZCHATSESSION": spk})
                self.update("ZWACHATSESSION", spk, {"ZPROPERTIES": ppk})

        members: dict[str, int] = self.existing_members(spk) if chat.is_group else {}
        if chat.is_group:
            for p in chat.participants:
                self._member(spk, members, p.jid, p.name or archive.contacts.get(p.jid), p.is_admin, p.active, archive)

        seen = self.existing_stanza_ids(spk)
        last_pk = last_text = None
        last_date = None
        count = 0
        for msg in messages:
            if msg.key_id in seen:
                rep.messages_skipped_duplicate += 1
                continue
            seen.add(msg.key_id)
            member_pk = None
            if chat.is_group and not msg.from_me and msg.sender_jid:
                member_pk = self._member(spk, members, msg.sender_jid, archive.contacts.get(msg.sender_jid),
                                         False, False, archive)
            mpk = self._write_message(chat, spk, msg, member_pk, archive, rep, media_root, include_media, media_jid)
            last_pk, last_date = mpk, to_cocoa(msg.timestamp_ms)
            last_text = msg.text if msg.kind is MsgKind.TEXT else last_text
            count += 1
            rep.messages_written += 1

        if count:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM ZWAMESSAGE WHERE ZCHATSESSION=?", (spk,)
            ).fetchone()[0]
            values = {"ZMESSAGECOUNTER": total}
            existing_last = session["ZLASTMESSAGEDATE"] if session else None
            # Only move the "last message" pointer forward: the iPhone may already hold a
            # message newer than all of the Android history.
            if existing_last is None or (last_date is not None and last_date >= existing_last):
                values.update({"ZLASTMESSAGE": last_pk, "ZLASTMESSAGEDATE": last_date})
                self.conn.execute("UPDATE ZWAMESSAGE SET ZLASTSESSION=NULL WHERE ZLASTSESSION=?", (spk,))
                self.update("ZWAMESSAGE", last_pk, {"ZLASTSESSION": spk})
                if not self.conv["lasttext_is_blob"]:
                    values["ZLASTMESSAGETEXT"] = last_text
            self.update("ZWACHATSESSION", spk, values)

    def renumber_sort(self) -> None:
        """Re-assign ZSORT in chronological order across the whole table, so imported history
        interleaves correctly with messages that already existed on the iPhone."""
        rows = self.conn.execute("SELECT Z_PK FROM ZWAMESSAGE ORDER BY ZMESSAGEDATE, Z_PK").fetchall()
        self.conn.executemany("UPDATE ZWAMESSAGE SET ZSORT=? WHERE Z_PK=?",
                              [(i, r[0]) for i, r in enumerate(rows, 1)])
        self._sort = len(rows)

    def _member(self, spk: int, members: dict[str, int], jid: str, name: str | None, admin: bool, active: bool,
                archive: Archive) -> int:
        """The member row for this person, by LID or phone JID, added if the group doesn't have them yet."""
        key = archive.phone_to_lid.get(jid, jid)
        known = members.get(key) or members.get(jid)
        if known:
            return known
        members[key] = self.insert("ZWAGROUPMEMBER", "WAGroupMember", {
            "ZCHATSESSION": spk,
            "ZMEMBERJID": key,
            "ZCONTACTNAME": name or jid.split("@")[0],
            "ZISACTIVE": 1 if active and key.endswith("@lid") else 0,
            "ZISADMIN": 1 if admin else 0,
        })
        return members[key]

    def _write_message(self, chat: Chat, spk: int, msg: Message, member_pk: int | None,
                       archive: Archive, rep: ImportReport, media_root: str | None,
                       include_media: bool, media_jid: str) -> int:
        self._sort += 1
        ztype = IOS_TYPE.get(msg.kind, 0)
        is_media = msg.kind in MEDIA_KINDS
        text = None if is_media else msg.text
        if msg.kind is MsgKind.DOCUMENT and msg.media:
            text = msg.media.file_name
        if msg.kind is MsgKind.REVOKED:
            text = None
        if msg.kind is MsgKind.LOCATION and not msg.location:
            ztype, text = 0, msg.text or "[Location]"
        if msg.kind is MsgKind.CONTACT:
            ztype, text = (4, None) if msg.vcard else (0, msg.text or "[Contact card]")

        sender_name = None
        if not msg.from_me and msg.sender_jid and not self.conv["pushname_is_blob"]:
            sender_name = archive.contacts.get(msg.sender_jid)   # legacy builds only: plain push name

        cv = self.conv
        values = {
            "ZCHATSESSION": spk,
            "ZISFROMME": 1 if msg.from_me else 0,
            "ZMESSAGETYPE": ztype,
            "ZMESSAGESTATUS": cv["msg_status_out"] if msg.from_me else cv["msg_status_in"],
            "ZMESSAGEERRORSTATUS": 0,
            "ZSORT": self._sort,
            "ZSTARRED": 1 if msg.starred else None,
            "ZFLAGS": cv["msg_flags_file"] if is_media else cv["msg_flags_out"] if msg.from_me else cv["msg_flags_in"],
            "ZSPOTLIGHTSTATUS": cv["msg_spot"],
            "ZDOCID": 0,
            "ZCHILDMESSAGESDELIVEREDCOUNT": 0,
            "ZCHILDMESSAGESPLAYEDCOUNT": 0,
            "ZCHILDMESSAGESREADCOUNT": 0,
            "ZDATAITEMVERSION": cv["msg_dataver"],
            "ZFILTEREDRECIPIENTCOUNT": 0,
            "ZENCRETRYCOUNT": 0,
            "ZGROUPEVENTTYPE": cv["msg_group_event"],
            "ZMESSAGEDATE": to_cocoa(msg.timestamp_ms),
            "ZSENTDATE": to_cocoa(msg.timestamp_ms) if msg.kind is MsgKind.REVOKED else None,
            "ZFROMJID": None if msg.from_me else media_jid,
            "ZTOJID": media_jid if msg.from_me else None,
            "ZSTANZAID": msg.key_id,
            "ZTEXT": text,
            "ZPUSHNAME": sender_name,
            "ZGROUPMEMBER": member_pk,
        }
        mpk = self.insert("ZWAMESSAGE", "WAMessage", values)

        media_pk = None
        needs_item = is_media or msg.quote or msg.kind in (MsgKind.LOCATION, MsgKind.CONTACT, MsgKind.REVOKED)
        if needs_item and self.has_table("ZWAMEDIAITEM"):
            media_pk = self._write_media_item(chat, mpk, msg, rep, media_root, include_media, media_jid, archive)
        if media_pk:
            self.update("ZWAMESSAGE", mpk, {"ZMEDIAITEM": media_pk})
        if msg.reactions and self.has_table("ZWAMESSAGEINFO") and "WAMessageInfo" in self.entities:
            receipts = encode_reactions(msg.reactions, archive.phone_to_lid)
            info_pk = self.insert("ZWAMESSAGEINFO", "WAMessageInfo", {"ZMESSAGE": mpk, "ZRECEIPTINFO": receipts})
            self.update("ZWAMESSAGE", mpk, {"ZMESSAGEINFO": info_pk})
            rep.reactions += len(msg.reactions)
        return mpk

    def _write_media_item(self, chat: Chat, mpk: int, msg: Message, rep: ImportReport, media_root: str | None,
                          include_media: bool, media_jid: str, archive: Archive) -> int | None:
        item: dict = {"ZMESSAGE": mpk, "ZMEDIAORIGIN": 0}
        if msg.quote:
            sender = archive.owner_jid if msg.quote.from_me else (msg.quote.sender_jid or chat.lid or chat.jid)
            if not sender:
                rep.replies_without_sender += 1
            item["ZMETADATA"] = encode_reply(msg.quote, archive.phone_to_lid.get(sender, sender))
            rep.replies += 1
        if msg.kind is MsgKind.REVOKED:
            item["ZTITLE"] = msg.revoked_key_id
            item["ZVCARDNAME"] = media_jid
            rep.revoked += 1
        elif msg.kind is MsgKind.LOCATION and msg.location:
            item.update({
                "ZLATITUDE": msg.location.latitude,
                "ZLONGITUDE": msg.location.longitude,
                "ZTITLE": msg.location.name,
            })
        elif msg.kind is MsgKind.CONTACT and msg.vcard:
            name = _vcard_name(msg.vcard) or msg.text
            item.update({"ZVCARDSTRING": msg.vcard, "ZVCARDNAME": name})
        elif msg.media is not None:
            self._fill_media(item, mpk, msg, msg.media, rep, media_root, include_media, media_jid)
        elif not msg.quote:
            return None
        return self.insert("ZWAMEDIAITEM", "WAMediaItem", item)

    def _fill_media(self, item: dict, mpk: int, msg: Message, m: Media, rep: ImportReport,
                    media_root: str | None, include_media: bool, media_jid: str) -> None:
        """A row is always written, so the bubble appears; only a file that really exists gets a path."""
        item.update({
            "ZFILESIZE": m.size or 0,
            "ZMOVIEDURATION": int(m.duration_s or 0),
            "ZTITLE": m.caption if m.caption != m.file_name else None,
            "ZVCARDSTRING": m.mime_type or _guess_mime(m, msg.kind),
            "ZVCARDNAME": m.file_hash,
        })
        if msg.kind is MsgKind.VOICE:
            item["ZMEDIAORIGIN"] = ORIGIN_VOICE
        if msg.kind is MsgKind.DOCUMENT:
            item["ZAUTHORNAME"] = m.file_name
        local = _locate_media(m, media_root) if (include_media and media_root) else None
        if not local:
            rep.media_missing += 1
            return
        db_path = media_db_path(media_jid, msg.key_id, os.path.splitext(local)[1].lower())
        item["ZMEDIALOCALPATH"] = db_path
        if msg.kind in GALLERY_KINDS:
            self.update("ZWAMESSAGE", mpk, {"ZMEDIASECTIONID": _media_section(msg.timestamp_ms)})
        rep.media_jobs.append(MediaJob(local_file=local, relative_path=manifest_path(db_path)))
        rep.media_linked += 1
        if msg.kind is MsgKind.IMAGE and self.thumb_dir:
            thumb_db_path = os.path.splitext(db_path)[0] + ".thumb"
            dest = os.path.join(self.thumb_dir, os.path.basename(thumb_db_path))
            if make_thumbnail(local, dest):
                item["ZXMPPTHUMBPATH"] = thumb_db_path
                rep.media_jobs.append(MediaJob(local_file=dest, relative_path=manifest_path(thumb_db_path)))
                rep.thumbnails += 1


def make_thumbnail(src: str, dest: str) -> bool:
    """A small JPEG, as WhatsApp's own `.thumb` files. Best effort: a message without one still works."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        from PIL import Image
    except ImportError:
        if not shutil.which("sips"):             # macOS built-in; Pillow elsewhere
            return False
        r = subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(THUMB_LONG_EDGE), src, "--out", dest],
                           capture_output=True)
        return r.returncode == 0 and os.path.isfile(dest)
    try:
        with Image.open(src) as im:
            im.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))
            im.convert("RGB").save(dest, "JPEG")
        return True
    except OSError:
        return False


def _guess_mime(m: Media, kind: MsgKind) -> str | None:
    return mimetypes.guess_type(m.android_path or "")[0] or DEFAULT_MIME.get(kind)


def _locate_media(m: Media, media_root: str) -> str | None:
    """Find a message's file under the pulled media folder. The content hash decides when there is one:
    paths aren't enough, because old messages keep regular-WhatsApp paths and file names repeat."""
    if m.file_hash:
        by_hash = _media_hash_index(media_root).get(m.file_hash)
        if by_hash:
            return by_hash
    if not m.android_path:
        return None
    rel = m.android_path.lstrip("/")
    if rel.startswith("Media/"):
        rel = rel[len("Media/"):]
    cand = os.path.join(media_root, rel)
    if os.path.isfile(cand):
        return cand
    # Same name elsewhere (Sent/ vs root). Only when no hash could have said otherwise.
    return None if m.file_hash else _media_index(media_root).get(os.path.basename(rel))


@functools.lru_cache(maxsize=4)
def _media_hash_index(media_root: str) -> dict[str, str]:
    index: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(media_root):
        for f in files:
            path = os.path.join(dirpath, f)
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            index.setdefault(base64.b64encode(digest.digest()).decode(), path)
    return index


@functools.lru_cache(maxsize=4)
def _media_index(media_root: str) -> dict[str, str]:
    index: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(media_root):
        for f in files:
            index.setdefault(f, os.path.join(dirpath, f))
    return index


def _media_section(ts_ms: int) -> str:
    """WhatsApp groups the media gallery by 'YYYY-MM' in ZMEDIASECTIONID."""
    import datetime as _dt

    d = _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc)
    return f"{d.year}-{d.month:02d}"


def _looks_like_base64_blob(value: str) -> bool:
    """Modern WhatsApp stores base64-encoded protobuf in a few former text columns."""
    import base64
    import re

    if not isinstance(value, str) or len(value) < 8 or " " in value:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:  # noqa: BLE001
        return False
    # protobuf: first byte is a field tag; real words would decode to junk with high bytes anyway
    return len(raw) >= 4 and raw[0] in range(0x08, 0x80)


def _vcard_name(vcard: str) -> str | None:
    for line in vcard.splitlines():
        if line.startswith("FN"):
            return line.split(":", 1)[-1].strip()
    return None
