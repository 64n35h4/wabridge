"""Runs with `pytest` or plain `python -m unittest discover tests`."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import fixtures  # noqa: E402

from wabridge import pipeline  # noqa: E402
from wabridge.android import adb, crypt15, msgstore  # noqa: E402
from wabridge.ios import backup as iosbackup  # noqa: E402
from wabridge.ios import chatstorage, probe  # noqa: E402
from wabridge.model import Message, MsgKind  # noqa: E402

KEY = "0123456789abcdef" * 4


class Crypt15Tests(unittest.TestCase):
    def test_parse_key_accepts_spaced_hex(self):
        spaced = " ".join(KEY[i:i + 8] for i in range(0, 64, 8))
        self.assertEqual(crypt15.parse_key(spaced), bytes.fromhex(KEY))

    def test_parse_key_rejects_garbage(self):
        with self.assertRaises(crypt15.Crypt15Error):
            crypt15.parse_key("not-a-key")

    def test_derive_is_hkdf_sha256(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        root = bytes.fromhex(KEY)
        ref = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"\x00" * 32,
                   info=b"backup encryption").derive(root)
        self.assertEqual(crypt15.derive_aes_key(root), ref)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "m.db")
            fixtures.make_msgstore(db)
            with open(db, "rb") as fh:
                plain = fh.read()
            enc = crypt15.encrypt_bytes(plain, KEY)
            self.assertNotIn(b"SQLite format 3", enc)
            self.assertEqual(crypt15.decrypt_bytes(enc, KEY), plain)
            # trailing 16-byte trailer variant is tolerated
            self.assertEqual(crypt15.decrypt_bytes(enc + b"\x00" * 16, KEY), plain)

    def test_iv_is_found_even_when_a_16_char_string_precedes_it(self):
        """A 16-byte version string in the prefix must not be mistaken for the IV."""
        import zlib

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        root = bytes.fromhex(KEY)
        iv = bytes(range(16))
        plain = b"SQLite format 3\x00" + b"z" * 64
        ct = AESGCM(crypt15.derive_aes_key(root)).encrypt(iv, zlib.compress(plain), None)
        version = b"2.25.12.75-beta1"                       # exactly 16 bytes, comes first
        inner_iv = bytes([0x0A, 16]) + iv
        header = bytes([0x12, 16]) + version + bytes([0x1A, len(inner_iv)]) + inner_iv
        enc = bytes([len(header)]) + header + ct
        h = crypt15.parse_header(enc)
        self.assertEqual(len(h.iv_candidates), 2)
        self.assertEqual(crypt15.decrypt_bytes(enc, KEY), plain)

    def test_wrong_key_fails_cleanly(self):
        enc = crypt15.encrypt_bytes(b"SQLite format 3\x00" + b"x" * 100, KEY)
        with self.assertRaises(crypt15.Crypt15Error):
            crypt15.decrypt_bytes(enc, "f" * 64)


class MsgstoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "msgstore.db")
        fixtures.make_msgstore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_chats_and_types(self):
        arc = msgstore.parse(self.db, contacts={"61400000002": "Alice"})
        by = {c.jid: c for c in arc.chats}
        self.assertEqual(set(by), {fixtures.ALICE, fixtures.GROUP})     # status@broadcast dropped
        alice = by[fixtures.ALICE]
        self.assertEqual(alice.name, "Alice")
        kinds = [m.kind for m in alice.messages]
        self.assertEqual(kinds, [MsgKind.TEXT, MsgKind.TEXT, MsgKind.IMAGE, MsgKind.VOICE, MsgKind.LOCATION,
                                 MsgKind.CONTACT, MsgKind.STICKER, MsgKind.DOCUMENT])
        self.assertEqual(alice.messages[1].quoted_key_id, "K100")
        self.assertTrue(alice.messages[1].starred)
        self.assertEqual(alice.messages[2].media.caption, "look at this")
        self.assertAlmostEqual(alice.messages[4].location.latitude, -37.81)
        self.assertIn("FN:Bob Example", alice.messages[5].vcard)

    def test_group_lid_mapping(self):
        arc = msgstore.parse(self.db)
        group = next(c for c in arc.chats if c.is_group)
        self.assertEqual(group.name, "Family")
        senders = [m.sender_jid for m in group.messages]
        self.assertEqual(senders, [fixtures.ALICE, fixtures.BOB_PN, None])   # LID resolved via jid_map
        self.assertEqual({p.jid for p in group.participants}, {fixtures.ME, fixtures.ALICE, fixtures.BOB_PN})
        self.assertTrue(next(p for p in group.participants if p.jid == fixtures.ME).is_admin)

    def test_include_system(self):
        arc = msgstore.parse(self.db, include_system=True, include_revoked=True)
        alice = next(c for c in arc.chats if c.jid == fixtures.ALICE)
        self.assertEqual(len(alice.messages), 10)

    def test_resolve_name_suffix(self):
        self.assertEqual(msgstore.resolve_name("61400000002@s.whatsapp.net", {"0400000002": "A"}), "A")
        self.assertIsNone(msgstore.resolve_name("x@g.us", {"0400000002": "A"}))


class ChatStorageWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.msg = os.path.join(self.tmp.name, "msgstore.db")
        self.cs = os.path.join(self.tmp.name, "ChatStorage.sqlite")
        self.media = os.path.join(self.tmp.name, "Media")
        fixtures.make_msgstore(self.msg)
        fixtures.make_chatstorage(self.cs)
        fixtures.make_media_tree(self.media)
        self.archive = msgstore.parse(self.msg, contacts={"61400000002": "Alice", "61400000003": "Bob"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_import(self):
        w = chatstorage.Writer(self.cs)
        rep = w.import_archive(self.archive, media_root=self.media)
        w.finalize()

        self.assertEqual(rep.sessions_reused, 1)          # Alice existed
        self.assertEqual(rep.sessions_created, 1)         # group
        self.assertEqual(rep.messages_skipped_duplicate, 1)  # K101 already on the iPhone
        self.assertEqual(rep.messages_written, 10)
        self.assertEqual(rep.media_linked, 4)
        self.assertEqual(rep.media_missing, 1)            # VID-001.mp4 not pulled
        self.assertEqual(len(rep.media_jobs), 4)
        self.assertTrue(all(j.relative_path.startswith("Message/Media/") for j in rep.media_jobs))
        self.assertEqual(rep.replies, 2)
        self.assertEqual(rep.replies_without_sender, 1)   # the fixture archive has no owner JID
        self.assertEqual(rep.reactions, 2)

        conn = sqlite3.connect(self.cs)
        conn.row_factory = sqlite3.Row
        # Core Data bookkeeping
        pk = {r["Z_NAME"]: r["Z_MAX"] for r in conn.execute("SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY")}
        self.assertEqual(pk["WAMessage"], conn.execute("SELECT MAX(Z_PK) FROM ZWAMESSAGE").fetchone()[0])
        self.assertEqual(pk["WAChatSession"], 2)
        self.assertEqual(pk["WAGroupMember"], conn.execute("SELECT COUNT(*) FROM ZWAGROUPMEMBER").fetchone()[0])
        ents = {r[0] for r in conn.execute("SELECT DISTINCT Z_ENT FROM ZWAMESSAGE")}
        self.assertEqual(ents, {7})
        # ZSORT strictly increasing, no duplicates
        sorts = [r[0] for r in conn.execute("SELECT ZSORT FROM ZWAMESSAGE ORDER BY Z_PK")]
        self.assertEqual(len(sorts), len(set(sorts)))
        # group session
        g = conn.execute("SELECT * FROM ZWACHATSESSION WHERE ZCONTACTJID=?", (fixtures.GROUP,)).fetchone()
        self.assertEqual(g["ZSESSIONTYPE"], 1)
        self.assertEqual(g["ZPARTNERNAME"], "Family")
        self.assertEqual(g["ZMESSAGECOUNTER"], 3)
        self.assertIsNotNone(g["ZGROUPINFO"])
        self.assertIsNotNone(g["ZLASTMESSAGE"])
        # incoming group message links to a member row with resolved LID
        bob_msg = conn.execute("SELECT * FROM ZWAMESSAGE WHERE ZSTANZAID='K111'").fetchone()
        member = conn.execute("SELECT * FROM ZWAGROUPMEMBER WHERE Z_PK=?", (bob_msg["ZGROUPMEMBER"],)).fetchone()
        self.assertEqual(member["ZMEMBERJID"], fixtures.BOB_LID)       # WhatsApp's active members are LIDs
        self.assertEqual(member["ZISACTIVE"], 1)
        self.assertEqual(member["ZCONTACTNAME"], "Bob")
        alice_member = conn.execute("SELECT * FROM ZWAGROUPMEMBER WHERE ZMEMBERJID=?", (fixtures.ALICE,)).fetchone()
        self.assertEqual(alice_member["ZISACTIVE"], 0)                 # no LID known: a phone row is never active
        self.assertEqual(bob_msg["ZFROMJID"], fixtures.GROUP)
        self.assertIsNone(bob_msg["ZPUSHNAME"])     # no real row proves plain text is safe here
        # outgoing message conventions
        mine = conn.execute("SELECT * FROM ZWAMESSAGE WHERE ZSTANZAID='K112'").fetchone()
        self.assertEqual(mine["ZISFROMME"], 1)
        self.assertEqual(mine["ZTOJID"], fixtures.GROUP)
        self.assertIsNone(mine["ZFROMJID"])
        self.assertEqual(mine["ZMESSAGETYPE"], 2)   # video
        # media item wiring
        img = conn.execute("SELECT * FROM ZWAMESSAGE WHERE ZSTANZAID='K102'").fetchone()
        self.assertEqual(img["ZMESSAGETYPE"], 1)
        self.assertIsNone(img["ZTEXT"])
        mi = conn.execute("SELECT * FROM ZWAMEDIAITEM WHERE Z_PK=?", (img["ZMEDIAITEM"],)).fetchone()
        self.assertEqual(mi["ZMESSAGE"], img["Z_PK"])
        self.assertEqual(mi["ZTITLE"], "look at this")
        self.assertEqual(mi["ZVCARDSTRING"], "image/jpeg")
        self.assertEqual(img["ZFLAGS"], chatstorage.FLAGS_FILE)
        self.assertEqual(img["ZMEDIASECTIONID"], "2024-09")
        self.assertEqual(mi["ZVCARDNAME"], fixtures.sha256_b64(fixtures.IMAGE_BYTES))
        # relative to Message/: the row and the backup path differ by exactly that prefix
        self.assertRegex(mi["ZMEDIALOCALPATH"], rf"^Media/{fixtures.ALICE}/(.)/(.)/\1\2[-0-9a-f]+\.jpg$")
        self.assertIn("Message/" + mi["ZMEDIALOCALPATH"], [j.relative_path for j in rep.media_jobs])
        text = conn.execute("SELECT ZFLAGS FROM ZWAMESSAGE WHERE ZSTANZAID='K100'").fetchone()[0]
        self.assertEqual(text, chatstorage.FLAGS_TEXT)
        voice = conn.execute("SELECT m.ZMESSAGETYPE, i.ZMEDIAORIGIN FROM ZWAMESSAGE m JOIN ZWAMEDIAITEM i "
                             "ON m.ZMEDIAITEM=i.Z_PK WHERE m.ZSTANZAID='K103'").fetchone()
        self.assertEqual(tuple(voice), (3, chatstorage.ORIGIN_VOICE))
        doc = conn.execute("SELECT g.ZTEXT, m.ZAUTHORNAME, m.ZVCARDSTRING FROM ZWAMEDIAITEM m "
                           "JOIN ZWAMESSAGE g ON g.ZMEDIAITEM=m.Z_PK WHERE g.ZSTANZAID='K109'").fetchone()
        self.assertEqual(tuple(doc), ("report.pdf", "report.pdf", "application/pdf"))
        loc = conn.execute(
            "SELECT m.ZLATITUDE FROM ZWAMEDIAITEM m JOIN ZWAMESSAGE g ON g.ZMEDIAITEM=m.Z_PK WHERE g.ZSTANZAID='K104'"
        ).fetchone()[0]
        self.assertAlmostEqual(loc, -37.81)
        card = conn.execute(
            "SELECT m.ZVCARDNAME FROM ZWAMEDIAITEM m JOIN ZWAMESSAGE g ON g.ZMEDIAITEM=m.Z_PK WHERE g.ZSTANZAID='K105'"
        ).fetchone()[0]
        self.assertEqual(card, "Bob Example")
        # Cocoa epoch conversion
        d = conn.execute("SELECT ZMESSAGEDATE FROM ZWAMESSAGE WHERE ZSTANZAID='K100'").fetchone()[0]
        self.assertAlmostEqual(d, 1_726_000_001 - 978307200, places=3)
        # single-file DB after finalize
        self.assertFalse(os.path.exists(self.cs + "-wal"))
        conn.close()

    def test_existing_newer_iphone_message_keeps_last_and_sort_order(self):
        # Make the pre-existing iPhone message (K101 in fixture) far newer than the Android history.
        conn = sqlite3.connect(self.cs)
        conn.execute("UPDATE ZWAMESSAGE SET ZMESSAGEDATE=800000000.0 WHERE Z_PK=1")
        conn.execute("UPDATE ZWACHATSESSION SET ZLASTMESSAGEDATE=800000000.0, ZLASTMESSAGE=1 WHERE Z_PK=1")
        conn.commit()
        conn.close()
        w = chatstorage.Writer(self.cs)
        w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        conn = sqlite3.connect(self.cs)
        self.assertEqual(conn.execute("SELECT ZLASTMESSAGE FROM ZWACHATSESSION WHERE Z_PK=1").fetchone()[0], 1)
        rows = conn.execute("SELECT ZMESSAGEDATE FROM ZWAMESSAGE ORDER BY ZSORT").fetchall()
        dates = [r[0] for r in rows]
        self.assertEqual(dates, sorted(dates))                       # ZSORT follows chronology
        self.assertEqual(conn.execute("SELECT MAX(ZSORT), COUNT(*) FROM ZWAMESSAGE").fetchone(), (11, 11))
        conn.close()

    def test_media_filenames_do_not_collide(self):
        self.assertNotEqual(
            chatstorage.media_db_path("x@g.us", "K1", ".jpg"),
            chatstorage.media_db_path("x@g.us", "K2", ".jpg"),
        )

    def _row(self, conn, key):
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT m.*, i.ZMETADATA, i.ZTITLE, i.ZMEDIALOCALPATH, i.ZVCARDSTRING, i.ZFILESIZE "
                            "FROM ZWAMESSAGE m LEFT JOIN ZWAMEDIAITEM i ON m.ZMEDIAITEM=i.Z_PK "
                            "WHERE m.ZSTANZAID=?", (key,)).fetchone()

    def test_missing_file_still_gets_a_bubble(self):
        w = chatstorage.Writer(self.cs)
        w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        conn = sqlite3.connect(self.cs)
        row = self._row(conn, "K112")               # VID-001.mp4 isn't on the phone
        self.assertEqual(row["ZMESSAGETYPE"], 2)
        self.assertIsNotNone(row["ZMEDIAITEM"])
        self.assertIsNone(row["ZMEDIALOCALPATH"])
        self.assertEqual(row["ZVCARDSTRING"], "video/mp4")
        self.assertEqual(row["ZFILESIZE"], 55555)
        self.assertEqual(row["ZFLAGS"], chatstorage.FLAGS_FILE)
        conn.close()

    def test_rows_follow_what_whatsapp_writes(self):
        w = chatstorage.Writer(self.cs)
        w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        conn = sqlite3.connect(self.cs)
        conn.row_factory = sqlite3.Row
        text = self._row(conn, "K100")
        self.assertEqual(text["ZGROUPEVENTTYPE"], chatstorage.EVENT_NONE)
        self.assertIsNone(text["ZSTARRED"])
        self.assertIsNone(text["ZSENTDATE"])
        self.assertIsNone(self._row(conn, "K112")["ZMEDIASECTIONID"])     # no file, so not in the gallery
        self.assertIsNone(self._row(conn, "K108")["ZMEDIASECTIONID"])     # stickers never are
        self.assertIsNone(self._row(conn, "K103")["ZMEDIASECTIONID"])     # nor voice notes
        contact = self._row(conn, "K105")
        self.assertIsNone(contact["ZTEXT"])
        self.assertEqual(contact["ZMESSAGETYPE"], 4)
        for session in conn.execute("SELECT Z_PK, ZLASTMESSAGE FROM ZWACHATSESSION").fetchall():
            marked = conn.execute("SELECT Z_PK FROM ZWAMESSAGE WHERE ZLASTSESSION=?", (session["Z_PK"],)).fetchall()
            self.assertEqual([r[0] for r in marked], [session["ZLASTMESSAGE"]])
        conn.close()

    def test_a_few_rows_are_not_a_convention(self):
        conn = sqlite3.connect(self.cs)
        conn.execute("UPDATE ZWAMESSAGE SET ZMESSAGESTATUS=1, ZFLAGS=7, ZGROUPEVENTTYPE=9")     # a single pending row
        conn.commit()
        conn.close()
        w = chatstorage.Writer(self.cs)
        self.assertEqual(w.conv["msg_status_out"], 8)
        self.assertEqual(w.conv["msg_flags_out"], chatstorage.FLAGS_TEXT)
        self.assertEqual(w.conv["msg_group_event"], chatstorage.EVENT_NONE)
        w.conn.close()

    def test_chat_with_only_group_events_creates_no_session(self):
        archive = msgstore.parse(self.msg)
        archive.chats[0].messages = [Message(key_id="S1", from_me=False, timestamp_ms=1, kind=MsgKind.SYSTEM)]
        archive.chats[0].jid = "6100000@s.whatsapp.net"
        w = chatstorage.Writer(self.cs)
        rep = w.import_archive(archive, media_root=self.media)
        w.finalize()
        self.assertEqual(rep.system_skipped, 1)
        conn = sqlite3.connect(self.cs)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ZWACHATSESSION WHERE ZCONTACTJID LIKE '6100000%'")
                         .fetchone()[0], 0)
        conn.close()

    def test_reply_metadata(self):
        self.archive.owner_jid = fixtures.ME
        w = chatstorage.Writer(self.cs)
        rep = w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        self.assertEqual(rep.replies_without_sender, 0)
        conn = sqlite3.connect(self.cs)
        text_reply = self._row(conn, "K111")
        self.assertEqual(text_reply["ZMESSAGETYPE"], 0)
        self.assertEqual(text_reply["ZTEXT"], "group msg from bob (lid)")
        fields = probe.pb_fields(text_reply["ZMETADATA"])
        self.assertEqual([f[0] for f in fields], [5, 6, 19, 211])
        self.assertEqual(fields[0][2], b"K110")
        self.assertEqual(fields[1][2], fixtures.ALICE.encode())
        self.assertEqual(probe.pb_fields(fields[2][2]), [(1, 2, b"group msg from alice")])
        self.assertEqual(fields[3][2], 0)
        own_quote = probe.pb_fields(self._row(conn, "K112")["ZMETADATA"])     # a reply on a media message
        self.assertEqual(own_quote[1][2], fixtures.ME.encode())
        self.assertEqual(probe.pb_fields(own_quote[2][2]), [(1, 2, b"Photo")])
        conn.close()

    def test_reactions(self):
        w = chatstorage.Writer(self.cs)
        w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        conn = sqlite3.connect(self.cs)
        row = self._row(conn, "K100")
        blob = conn.execute("SELECT ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE Z_PK=? AND ZMESSAGE=?",
                            (row["ZMESSAGEINFO"], row["Z_PK"])).fetchone()[0]
        (number, _wire, value), = probe.pb_fields(blob)
        self.assertEqual(number, 7)
        items = [dict((f[0], f[2]) for f in probe.pb_fields(i[2])) for i in probe.pb_fields(value)]
        self.assertEqual(items[0][1], b"R1")
        self.assertEqual(items[0][2], fixtures.ALICE.encode())
        self.assertEqual(items[0][3].decode(), "❤️")
        self.assertEqual(items[0][4], 1_726_000_001_500)
        self.assertNotIn(6, items[0])
        self.assertNotIn(2, items[1])              # my own reaction names no sender, it sets field 6
        self.assertEqual(items[1][6], 1)
        self.assertIsNone(self._row(conn, "K102")["ZMESSAGEINFO"])   # a reaction taken back leaves nothing
        pk = conn.execute("SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME='WAMessageInfo'").fetchone()[0]
        self.assertEqual(pk, 1)
        conn.close()

    def test_group_already_synced_at_login(self):
        # WhatsApp lists the group's members by LID as soon as the account logs in.
        conn = sqlite3.connect(self.cs)
        conn.execute("INSERT INTO ZWACHATSESSION (Z_PK, Z_ENT, Z_OPT, ZCONTACTJID, ZSESSIONTYPE) VALUES (9, 2, 1, ?, 1)",
                     (fixtures.GROUP,))
        conn.execute("INSERT INTO ZWAGROUPMEMBER (Z_PK, Z_ENT, Z_OPT, ZCHATSESSION, ZMEMBERJID, ZISACTIVE, ZISADMIN) "
                     "VALUES (5, ?, 1, 9, ?, 1, 0)", (5, fixtures.BOB_LID))
        conn.commit()
        conn.close()
        w = chatstorage.Writer(self.cs)
        w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        conn = sqlite3.connect(self.cs)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT Z_PK, ZMEMBERJID FROM ZWAGROUPMEMBER WHERE ZCHATSESSION=9").fetchall()
        jids = [r["ZMEMBERJID"] for r in rows]
        self.assertEqual(jids.count(fixtures.BOB_LID), 1)
        self.assertNotIn(fixtures.BOB_PN, jids)
        self.assertEqual(self._row(conn, "K111")["ZGROUPMEMBER"], 5)
        conn.close()

    def test_account_placeholder_is_not_a_group_member(self):
        conn = sqlite3.connect(self.msg)
        conn.execute("INSERT INTO jid VALUES (90, 'lid_me', 'lid_me', 0, 0, 11, 'lid_me')")
        conn.execute("INSERT INTO group_participant_user VALUES (90, 5, 90, 2, 0)")
        conn.commit()
        conn.close()
        group = next(c for c in msgstore.parse(self.msg).chats if c.is_group)
        self.assertTrue(all("@" in p.jid for p in group.participants))

    def test_lid_session_uses_its_lid_everywhere(self):
        lid = "99887766@lid"
        conn = sqlite3.connect(self.cs)
        conn.execute("UPDATE ZWACHATSESSION SET ZCONTACTJID=?, ZCONTACTIDENTIFIER=? WHERE ZCONTACTJID=?",
                     (lid, fixtures.ALICE, fixtures.ALICE))
        conn.commit()
        conn.close()
        archive = msgstore.parse(self.msg, contacts=self.archive.contacts, include_revoked=True)
        archive.phone_to_lid[fixtures.ALICE] = lid
        w = chatstorage.Writer(self.cs)
        w.import_archive(archive, media_root=self.media)
        w.finalize()
        conn = sqlite3.connect(self.cs)
        conn.row_factory = sqlite3.Row
        row = self._row(conn, "K100")
        self.assertEqual(row["ZFROMJID"], lid)
        self.assertEqual(self._row(conn, "K103")["ZTOJID"], lid)
        revoked = conn.execute("SELECT i.ZVCARDNAME FROM ZWAMEDIAITEM i JOIN ZWAMESSAGE m ON m.ZMEDIAITEM=i.Z_PK "
                               "WHERE m.ZSTANZAID='K107'").fetchone()
        self.assertEqual(revoked[0], lid)                               # a deleted message names its chat
        blob = conn.execute("SELECT ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE Z_PK=?", (row["ZMESSAGEINFO"],)).fetchone()[0]
        reaction = dict((f[0], f[2]) for f in probe.pb_fields(probe.pb_fields(probe.pb_fields(blob)[0][2])[0][2]))
        self.assertEqual(reaction[2], lid.encode())
        conn.close()

    def test_revoked_message(self):
        archive = msgstore.parse(self.msg, include_revoked=True)
        w = chatstorage.Writer(self.cs)
        rep = w.import_archive(archive, media_root=self.media)
        w.finalize()
        self.assertEqual(rep.revoked, 1)
        conn = sqlite3.connect(self.cs)
        row = self._row(conn, "K107")
        self.assertEqual(row["ZMESSAGETYPE"], 14)
        self.assertIsNone(row["ZTEXT"])
        self.assertEqual(row["ZTITLE"], "ORIG107")
        self.assertIsNone(row["ZMETADATA"])
        conn.close()

    def test_group_events_are_not_written(self):
        archive = msgstore.parse(self.msg, include_system=True)
        w = chatstorage.Writer(self.cs)
        rep = w.import_archive(archive, media_root=self.media)
        w.finalize()
        self.assertEqual(rep.system_skipped, 1)
        conn = sqlite3.connect(self.cs)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE WHERE ZSTANZAID='K106'").fetchone()[0], 0)
        conn.close()

    def test_file_found_by_content_hash_wherever_it_is(self):
        # The database still says WhatsApp Images; the file is in the Business folder under another name.
        moved = os.path.join(self.media, "WhatsApp Business Images")
        os.makedirs(moved)
        shutil.move(os.path.join(self.media, "WhatsApp Images", "IMG-001.jpg"), os.path.join(moved, "other.jpg"))
        chatstorage._media_hash_index.cache_clear()
        w = chatstorage.Writer(self.cs)
        rep = w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        self.assertTrue(any(j.local_file.endswith("other.jpg") for j in rep.media_jobs))

    def test_same_file_name_with_another_hash_is_not_linked(self):
        other = os.path.join(self.media, "WhatsApp Images", "Sent")
        os.makedirs(other)
        shutil.move(os.path.join(self.media, "WhatsApp Images", "IMG-001.jpg"), os.path.join(other, "IMG-001.jpg"))
        with open(os.path.join(other, "IMG-001.jpg"), "wb") as fh:
            fh.write(b"somebody else's photo")
        chatstorage._media_hash_index.cache_clear()
        chatstorage._media_index.cache_clear()
        w = chatstorage.Writer(self.cs)
        rep = w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        self.assertEqual(rep.media_linked, 3)
        self.assertEqual(rep.media_missing, 2)
        conn = sqlite3.connect(self.cs)
        self.assertIsNone(self._row(conn, "K102")["ZMEDIALOCALPATH"])
        self.assertIsNotNone(self._row(conn, "K102")["ZMEDIAITEM"])
        conn.close()

    def test_unproven_columns_stay_empty_when_no_row_shows_them(self):
        w = chatstorage.Writer(self.cs)
        self.assertTrue(w.conv["lasttext_is_blob"])
        self.assertTrue(w.conv["pushname_is_blob"])
        w.conn.close()


    def test_import_is_idempotent(self):
        w = chatstorage.Writer(self.cs)
        w.import_archive(self.archive, media_root=self.media)
        w.finalize()
        w2 = chatstorage.Writer(self.cs)
        rep2 = w2.import_archive(self.archive, media_root=self.media)
        w2.finalize()
        self.assertEqual(rep2.messages_written, 0)
        self.assertEqual(rep2.sessions_created, 0)


class AdbMediaFolderTests(unittest.TestCase):
    def test_business_and_regular_names(self):
        listing = "\n".join([" AI Media", ".Links", "WallPaper", "WhatsApp Business Images",
                             "WhatsApp Business Stickers", "WhatsApp Images"])
        with mock.patch.object(adb, "shell", return_value=listing):
            self.assertEqual(adb.media_dirs("/root"),
                             ["WhatsApp Images", "WhatsApp Business Images", "WhatsApp Business Stickers"])

    def test_detect_products(self):
        def shell(cmd, serial=None, check=True):
            return "msgstore.db.crypt15" if "w4b" in cmd else ""

        with mock.patch.object(adb, "shell", shell):
            self.assertEqual(list(adb.detect_products()), ["business"])
            self.assertIn("w4b", adb.detect_wa_root(business=True))
            with self.assertRaisesRegex(adb.AdbError, "WhatsApp's Databases"):
                adb.detect_wa_root()

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cs = os.path.join(self.tmp.name, "ChatStorage.sqlite")
        fixtures.make_chatstorage(self.cs)
        self.root = fixtures.make_ios_backup(os.path.join(self.tmp.name, "backup", "UDID1"), self.cs)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_id_matches_known_value(self):
        self.assertEqual(iosbackup.file_id(iosbackup.WA_GROUP_DOMAIN, "ChatStorage.sqlite"),
                         "7c7fba66680ef796b916b067077cc246adacf01d")

    def test_find_backup_dir(self):
        self.assertEqual(iosbackup.find_backup_dir(os.path.join(self.tmp.name, "backup")), self.root)

    def test_extract_put_remove(self):
        with iosbackup.Backup(self.root) as b:
            self.assertTrue(b.has_whatsapp())
            out = b.extract_chatstorage(os.path.join(self.tmp.name, "x"))
            self.assertTrue(os.path.isfile(out))
            self.assertTrue(os.path.isfile(out + "-wal"))

            # replace ChatStorage with a bigger file; Size in the MBFile must follow
            with open(out, "ab") as fh:
                fh.write(b"\x00" * 4096)
            b.put(iosbackup.WA_GROUP_DOMAIN, iosbackup.CHATSTORAGE, out)
            entry = b.get(iosbackup.WA_GROUP_DOMAIN, iosbackup.CHATSTORAGE)
            meta = iosbackup.read_mbfile(entry.blob)
            self.assertEqual(meta["Size"], os.path.getsize(out))
            self.assertEqual(meta["RelativePath"], "ChatStorage.sqlite")
            self.assertEqual(meta["InodeNumber"], 424242)     # kept from original row

            # add a media file: directories are created, blob written to <xx>/<fileID>
            media = os.path.join(self.tmp.name, "IMG-1.jpg")
            with open(media, "wb") as fh:
                fh.write(b"jpeg" * 100)
            rel = "Message/Media/61400000002@s.whatsapp.net/a/b/IMG-1.jpg"
            fid = b.put(iosbackup.WA_GROUP_DOMAIN, rel, media, mtime=1_700_000_100)
            self.assertTrue(os.path.isfile(b.blob_path(fid)))
            e = b.get(iosbackup.WA_GROUP_DOMAIN, rel)
            self.assertEqual(e.flags, 1)
            m = iosbackup.read_mbfile(e.blob)
            self.assertEqual((m["Size"], m["Mode"], m["LastModified"]), (400, 0o100644, 1_700_000_100))
            self.assertEqual(m["RelativePath"], rel)
            self.assertGreater(m["InodeNumber"], 424244)
            for d in ("Message", "Message/Media", "Message/Media/61400000002@s.whatsapp.net",
                      "Message/Media/61400000002@s.whatsapp.net/a",
                      "Message/Media/61400000002@s.whatsapp.net/a/b"):
                de = b.get(iosbackup.WA_GROUP_DOMAIN, d)
                self.assertIsNotNone(de, d)
                self.assertEqual(de.flags, 2)
                self.assertEqual(iosbackup.read_mbfile(de.blob)["Mode"], 0o040775)
            self.assertEqual(b.conn.execute(
                "SELECT COUNT(*) FROM Files WHERE relativePath='Message'").fetchone()[0], 1)

            self.assertTrue(b.remove(iosbackup.WA_GROUP_DOMAIN, iosbackup.CHATSTORAGE + "-wal"))
            self.assertIsNone(b.get(iosbackup.WA_GROUP_DOMAIN, iosbackup.CHATSTORAGE + "-wal"))
            b.touch_status()

    def test_exception_rolls_back_manifest_changes(self):
        with self.assertRaises(RuntimeError):
            with iosbackup.Backup(self.root) as b:
                b.remove(iosbackup.WA_GROUP_DOMAIN, iosbackup.CHATSTORAGE + "-wal")
                raise RuntimeError("boom")
        with iosbackup.Backup(self.root) as b:
            self.assertIsNotNone(b.get(iosbackup.WA_GROUP_DOMAIN, iosbackup.CHATSTORAGE + "-wal"))

    def test_digest_written_for_new_files(self):
        media = os.path.join(self.tmp.name, "f.bin")
        with open(media, "wb") as fh:
            fh.write(b"data")
        with iosbackup.Backup(self.root) as b:
            b.put(iosbackup.WA_GROUP_DOMAIN, "Message/Media/x/f.bin", media)
            m = iosbackup.read_mbfile(b.get(iosbackup.WA_GROUP_DOMAIN, "Message/Media/x/f.bin").blob)
            self.assertNotIn("Digest", m)   # iOS writes none in unencrypted backups

    def test_encrypted_backup_refused(self):
        import plistlib
        p = os.path.join(self.root, "Manifest.plist")
        with open(p, "wb") as fh:
            plistlib.dump({"IsEncrypted": True}, fh)
        with self.assertRaises(iosbackup.BackupError):
            iosbackup.Backup(self.root)


class PipelineOfflineTests(unittest.TestCase):
    """convert + inject without any device: simulates what `android pull` and `ios backup` produce."""

    def test_convert_then_inject(self):
        with tempfile.TemporaryDirectory() as d:
            work = pipeline.Work(os.path.join(d, "work"))
            os.makedirs(work.path("android", "Databases"))
            fixtures.make_msgstore(work.msgstore_db)
            fixtures.make_media_tree(work.path("android", "Media"))
            with open(work.path("android", "contacts.json"), "w") as fh:
                json.dump({"61400000002": "Alice"}, fh)
            cs = os.path.join(d, "ChatStorage.sqlite")
            fixtures.make_chatstorage(cs)
            folder = fixtures.make_ios_backup(os.path.join(work.backup_root, "UDID1"), cs)

            # also exercise the decrypt stage with our own encoder
            with open(work.msgstore_db, "rb") as fh:
                enc = crypt15.encrypt_bytes(fh.read(), KEY)
            with open(work.path("android", "Databases", "msgstore.db.crypt15"), "wb") as fh:
                fh.write(enc)
            os.remove(work.msgstore_db)
            logs: list[str] = []
            pipeline.android_decrypt(work, KEY, out=logs.append)
            self.assertTrue(os.path.isfile(work.msgstore_db))

            rep = pipeline.convert(work, backup_folder=folder, out=logs.append)
            self.assertEqual(rep.messages_written, 11)       # 10 + the deleted-message marker
            self.assertEqual(rep.revoked, 1)
            self.assertEqual(rep.sessions_created, 1)
            pipeline.inject(work, backup_folder=folder, out=logs.append)

            with iosbackup.Backup(folder) as b:
                entries = b.list_domain(iosbackup.WA_GROUP_DOMAIN, "Message/Media/")
                files = [e for e in entries if e.flags == 1]
                self.assertEqual(len(files), 4)
                for e in files:
                    self.assertTrue(os.path.isfile(b.blob_path(e.file_id)))
                self.assertIsNone(b.get(iosbackup.WA_GROUP_DOMAIN, "ChatStorage.sqlite-wal"))
                # the injected ChatStorage is the converted one
                cs_entry = b.get(iosbackup.WA_GROUP_DOMAIN, iosbackup.CHATSTORAGE)
                conn = sqlite3.connect(b.blob_path(cs_entry.file_id))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0], 12)
                conn.close()
            self.assertIn("inject", work.state)
            self.assertTrue(any("media: 4 linked" in line for line in logs))

            # a backup outside the work dir is never modified
            outside = fixtures.make_ios_backup(os.path.join(d, "finder-backup", "UDID9"), cs)
            with self.assertRaises(pipeline.PipelineError):
                pipeline.inject(work, backup_folder=outside, out=logs.append)

    def _migrated(self, d: str, *, business: bool = False) -> tuple[pipeline.Work, str]:
        """A work folder that has run convert + inject, with an untouched pristine copy."""
        work = pipeline.Work(os.path.join(d, "work"))
        os.makedirs(work.path("android", "Databases"))
        fixtures.make_msgstore(work.msgstore_db)
        fixtures.make_media_tree(work.path("android", "Media"))
        cs = os.path.join(d, "ChatStorage.sqlite")
        fixtures.make_chatstorage(cs)
        folder = fixtures.make_ios_backup(os.path.join(work.backup_root, "UDID1"), cs, business=business)
        with iosbackup.Backup(folder) as b:
            b.put(iosbackup.WA_GROUP_DOMAIN, "Library/other-app.plist", cs)
        pristine = os.path.join(work.path("ios_backup_pristine"), "UDID1")
        shutil.copytree(folder, pristine)
        work.save(android_pull={"business": business}, ios_backup={"folder": folder, "pristine": pristine})
        return work, folder

    def test_inject_verifies_and_restore_needs_it(self):
        with tempfile.TemporaryDirectory() as d:
            work, folder = self._migrated(d)
            logs: list[str] = []
            pipeline.convert(work, out=logs.append)
            with self.assertRaisesRegex(pipeline.PipelineError, "verification"):
                pipeline.ios_restore(work, out=logs.append)
            pipeline.inject(work, out=logs.append)
            self.assertTrue(work.state["verify"]["ok"])
            with mock.patch.object(pipeline.device, "restore") as restore:
                pipeline.ios_restore(work, out=logs.append)
            restore.assert_called_once()

    def test_verify_catches_a_change_outside_the_target(self):
        with tempfile.TemporaryDirectory() as d:
            work, folder = self._migrated(d)
            logs: list[str] = []
            pipeline.convert(work, out=logs.append)
            pipeline.inject(work, out=logs.append)
            with iosbackup.Backup(folder) as b:
                entry = b.get(iosbackup.WA_GROUP_DOMAIN, "Library/other-app.plist")
                path = b.blob_path(entry.file_id)
            with open(path, "ab") as fh:
                fh.write(b"x")
            with self.assertRaisesRegex(pipeline.PipelineError, "content differs: .*other-app"):
                pipeline.verify(work, out=logs.append)
            self.assertFalse(work.state["verify"].get("ok"))
            with self.assertRaises(pipeline.PipelineError):
                pipeline.ios_restore(work, out=logs.append)

    def test_verify_catches_a_new_file_in_another_app(self):
        with tempfile.TemporaryDirectory() as d:
            work, folder = self._migrated(d)
            logs: list[str] = []
            pipeline.convert(work, out=logs.append)
            pipeline.inject(work, out=logs.append)
            with iosbackup.Backup(folder) as b:
                b.put("AppDomain-some.other.app", "Documents/x", work.msgstore_db)
            with self.assertRaisesRegex(pipeline.PipelineError, "added outside"):
                pipeline.verify(work, out=logs.append)

    def test_business_writes_only_to_its_own_domain(self):
        with tempfile.TemporaryDirectory() as d:
            work, folder = self._migrated(d, business=True)
            logs: list[str] = []
            pipeline.convert(work, out=logs.append)
            pipeline.inject(work, out=logs.append)
            self.assertEqual(work.state["convert"]["domain"], iosbackup.WA_BUSINESS_GROUP_DOMAIN)
            self.assertTrue(work.state["verify"]["ok"])
            with iosbackup.Backup(folder, read_only=True) as b:
                self.assertTrue(b.list_domain(iosbackup.WA_BUSINESS_GROUP_DOMAIN, "Message/Media/"))
                self.assertFalse(b.list_domain(iosbackup.WA_GROUP_DOMAIN, "Message/Media/"))

    def test_owner_comes_from_the_target_app(self):
        with tempfile.TemporaryDirectory() as d:
            work, folder = self._migrated(d, business=True)
            domain = iosbackup.WA_BUSINESS_GROUP_DOMAIN
            prefs = os.path.join(d, "prefs.plist")
            with open(prefs, "wb") as fh:
                plistlib.dump({"LastOwnJabberID": fixtures.ME}, fh)
            with iosbackup.Backup(folder) as b:
                b.put(domain, "Library/Preferences/group.net.whatsapp.WhatsAppSMB.shared.plist", prefs)
            with iosbackup.Backup(folder, read_only=True) as b:
                self.assertEqual(b.owner_jid(domain), fixtures.ME)

    def test_restore_payload_holds_only_what_changed(self):
        with tempfile.TemporaryDirectory() as d:
            work, folder = self._migrated(d, business=True)
            logs: list[str] = []
            pipeline.convert(work, out=logs.append)
            pipeline.inject(work, out=logs.append)
            scoped = work.state["verify"]["scoped"]
            with iosbackup.Backup(scoped, read_only=True) as b:
                rows = b.conn.execute("SELECT domain, relativePath, flags FROM Files").fetchall()
                self.assertEqual({r["domain"] for r in rows}, {iosbackup.WA_BUSINESS_GROUP_DOMAIN})
                names = {r["relativePath"] for r in rows}
                self.assertTrue({"", "ChatStorage.sqlite", "ChatStorage.sqlite-wal", "ChatStorage.sqlite-shm",
                                 "Message", "Message/Media"} <= names)   # "" is the app's root: iOS needs it
                self.assertFalse([n for n in names if "other-app" in n])
                for r in rows:
                    if r["flags"] == iosbackup.FLAG_FILE:
                        entry = b.get(r["domain"], r["relativePath"])
                        self.assertTrue(os.path.isfile(b.blob_path(entry.file_id)), r["relativePath"])
                wal = b.get(iosbackup.WA_BUSINESS_GROUP_DOMAIN, "ChatStorage.sqlite-wal")
                self.assertEqual(os.path.getsize(b.blob_path(wal.file_id)), 0)
            with mock.patch.object(pipeline.device, "restore") as restore:
                pipeline.ios_restore(work, out=logs.append)
            self.assertEqual(restore.call_args[0][0], os.path.dirname(scoped))
            self.assertTrue(restore.call_args[1]["skip_apps"])
            with open(os.path.join(scoped, "Info.plist"), "rb") as fh:
                self.assertNotIn("Applications", plistlib.load(fh))

    def test_rollback_refuses_a_backup_without_regular_whatsapps_chats(self):
        with tempfile.TemporaryDirectory() as d:
            work, _folder = self._migrated(d, business=True)        # the fixture has Business only
            with open(os.path.join(work.state["ios_backup"]["pristine"], "Info.plist"), "wb") as fh:
                plistlib.dump({"Installed Applications": ["net.whatsapp.WhatsApp"]}, fh)
            with mock.patch.object(pipeline.device, "restore") as restore:
                with self.assertRaisesRegex(pipeline.PipelineError, "regular WhatsApp"):
                    pipeline.ios_rollback(work, out=lambda *_: None)
            restore.assert_not_called()

    def test_refuses_a_chat_database_with_no_real_rows(self):
        with tempfile.TemporaryDirectory() as d:
            work, folder = self._migrated(d)
            cs = os.path.join(d, "empty.sqlite")
            fixtures.make_chatstorage(cs, with_existing_alice=False)
            with iosbackup.Backup(folder) as b:
                b.put(iosbackup.WA_GROUP_DOMAIN, iosbackup.CHATSTORAGE, cs)
            shutil.rmtree(work.state["ios_backup"]["pristine"])
            shutil.copytree(folder, work.state["ios_backup"]["pristine"])
            with self.assertRaisesRegex(pipeline.PipelineError, "hasn't written any messages"):
                pipeline.convert(work, out=lambda *_: None)

    def test_crypt14_only_explains_the_way_forward(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "msgstore.db.crypt14"), "w").close()
            with self.assertRaisesRegex(pipeline.PipelineError, "64-digit key"):
                pipeline.newest_crypt15(d)

    def test_probe_reports_structure_and_no_message_text(self):
        with tempfile.TemporaryDirectory() as d:
            work, folder = self._migrated(d)
            logs: list[str] = []
            pipeline.convert(work, out=logs.append)
            pipeline.inject(work, out=logs.append)
            text = probe.report(folder)
            self.assertIn("ZMEDIALOCALPATH: 4 rows, first segment [('Media', 4)]", text)
            self.assertIn("file exists as stored 0, under Message/ 4", text)
            self.assertIn("reactions: 2", text)
            self.assertNotIn("hi from alice", text)
            self.assertNotIn(fixtures.ALICE, text)

    def test_msgstore_path_with_special_chars(self):
        with tempfile.TemporaryDirectory() as d:
            weird = os.path.join(d, "what? 100%ab#1")
            os.makedirs(weird)
            db = os.path.join(weird, "msgstore.db")
            fixtures.make_msgstore(db)
            self.assertEqual(len(msgstore.parse(db).chats), 2)
            self.assertEqual(sorted(os.listdir(weird)), ["msgstore.db"])   # nothing created by the URI


class CliTests(unittest.TestCase):
    def test_help_and_version(self):
        from wabridge import cli
        with self.assertRaises(SystemExit) as cm:
            cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        parser = cli.build_parser()
        self.assertIn("migrate", parser.format_help())


if __name__ == "__main__":
    unittest.main()
