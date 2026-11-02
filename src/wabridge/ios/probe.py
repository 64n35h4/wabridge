"""Read-only report on the WhatsApp data in a backup, for checking what WhatsApp itself writes.

It works on temporary copies, and prints structure and counts only: no message text, no names, and
JIDs reduced to their server. That makes the report safe to share, which is the point: every fact in
chatstorage.py's docstring can be re-measured on a new iOS or WhatsApp build with this.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import sqlite3
import tempfile

from .backup import CHATSTORAGE, Backup

DOMAINS = {"WhatsApp": "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
           "WhatsApp Business": "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared"}


def pb_fields(data: bytes) -> list[tuple[int, int, object]] | None:
    """Decode one protobuf level as (field, wire type, value); None if it isn't protobuf."""
    out: list[tuple[int, int, object]] = []
    i = 0

    def varint(i: int) -> tuple[int, int]:
        shift = value = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value, i

    try:
        while i < len(data):
            tag, i = varint(i)
            number, wire = tag >> 3, tag & 7
            if wire == 0:
                value, i = varint(i)
                out.append((number, wire, value))
            elif wire == 2:
                length, i = varint(i)
                if i + length > len(data):
                    return None
                out.append((number, wire, data[i:i + length]))
                i += length
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
                out.append((number, wire, data[i:i + size]))
                i += size
            else:
                return None
    except IndexError:
        return None
    return out


def _server(jid: str | None) -> str:
    return jid[jid.index("@"):] if jid and "@" in jid else "none"


def _store_report(conn: sqlite3.Connection, prefix: str, paths: set[str]) -> list[str]:
    def q(sql: str, *args) -> list:
        return conn.execute(sql, args).fetchall()

    lines = [f"  messages: {q('SELECT COUNT(*) FROM ZWAMESSAGE')[0][0]}",
             f"  sessions by type: {q('SELECT ZSESSIONTYPE, COUNT(*) FROM ZWACHATSESSION GROUP BY 1')}",
             f"  ZMESSAGETYPE: {q('SELECT ZMESSAGETYPE, COUNT(*) FROM ZWAMESSAGE GROUP BY 1 ORDER BY 2 DESC')}"]

    flags: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for mtype, value, count in q("SELECT ZMESSAGETYPE, ZFLAGS, COUNT(*) FROM ZWAMESSAGE GROUP BY 1, 2"):
        flags[mtype][hex(value or 0)] = count
    lines.append("  ZFLAGS by type: " + str({t: c.most_common(2) for t, c in sorted(flags.items())}))

    for column in ("ZMEDIALOCALPATH", "ZXMPPTHUMBPATH"):
        stored = as_is = under_message = 0
        heads: collections.Counter = collections.Counter()
        for (value,) in q(f"SELECT {column} FROM ZWAMEDIAITEM WHERE {column} IS NOT NULL AND {column} <> ''"):
            stored += 1
            heads[value.split("/", 1)[0]] += 1
            as_is += prefix + value in paths
            under_message += f"{prefix}Message/{value}" in paths
        lines.append(f"  {column}: {stored} rows, first segment {heads.most_common(3)}, "
                     f"file exists as stored {as_is}, under Message/ {under_message}")

    mime: collections.Counter = collections.Counter()
    for mtype, value, count in q("SELECT m.ZMESSAGETYPE, i.ZVCARDSTRING, COUNT(*) FROM ZWAMEDIAITEM i "
                                 "JOIN ZWAMESSAGE m ON i.ZMESSAGE = m.Z_PK GROUP BY 1, 2"):
        kind = "null" if value is None else "mime" if re.fullmatch(r"[a-z]+/[-+.\w]+(; ?.*)?", value) else "other"
        mime[(mtype, kind)] += count
    lines.append(f"  ZVCARDSTRING by (type, kind): {mime.most_common(12)}")

    fields: collections.Counter = collections.Counter()
    for (blob,) in q("SELECT ZMETADATA FROM ZWAMEDIAITEM WHERE ZMETADATA IS NOT NULL"):
        fields.update(n for n, _w, _v in pb_fields(bytes(blob)) or [])
    lines.append(f"  ZMETADATA fields: {fields.most_common(12)}")

    reactions = 0
    senders: collections.Counter = collections.Counter()
    for (blob,) in q("SELECT ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE ZRECEIPTINFO IS NOT NULL"):
        for number, _wire, value in pb_fields(bytes(blob)) or []:
            if number != 7:
                continue
            for _n, _w, item in pb_fields(value) or []:
                reactions += 1
                for n, _w2, v in pb_fields(item) or []:
                    if n == 2:
                        senders[_server(v.decode(errors="replace"))] += 1
    lines.append(f"  reactions: {reactions}, sender forms {senders.most_common(3)}")
    events = q("SELECT ZGROUPEVENTTYPE, COUNT(*) FROM ZWAMESSAGE WHERE ZMESSAGETYPE = 6 GROUP BY 1")
    lines.append(f"  group events: {events}")
    return lines


def report(backup_dir: str) -> str:
    lines: list[str] = []
    with Backup(backup_dir, read_only=True) as b, tempfile.TemporaryDirectory() as tmp:
        for app, domain in DOMAINS.items():
            entries = b.list_domain(domain)
            paths = {e.relative_path for e in entries}
            stores = sorted(p for p in paths if p == CHATSTORAGE or re.fullmatch(rf"data/[^/]+/{CHATSTORAGE}", p))
            lines.append(f"{app}: {len(entries)} files, {len(stores)} account(s) with a chat database")
            for index, rel in enumerate(stores):
                prefix = rel[:-len(CHATSTORAGE)]
                lines.append(f" account {index + 1}" + (" (added account)" if prefix else ""))
                dest = os.path.join(tmp, f"{app}-{index}.sqlite")
                entry = b.get(domain, rel)
                shutil.copyfile(b.blob_path(entry.file_id), dest)
                wal = b.get(domain, rel + "-wal")
                if wal and os.path.isfile(b.blob_path(wal.file_id)):
                    shutil.copyfile(b.blob_path(wal.file_id), dest + "-wal")
                conn = sqlite3.connect(dest)      # a throwaway copy, so reading the WAL here is harmless
                try:
                    lines += _store_report(conn, prefix, paths)
                finally:
                    conn.close()
    return "\n".join(lines)
