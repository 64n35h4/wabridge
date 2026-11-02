"""Orchestrates the end-to-end migration with resumable checkpoints.

Everything lives under one work directory:

    <work>/state.json                 what has been completed
    <work>/android/Databases/…        pulled from the phone
    <work>/android/Media/…            pulled media (optional)
    <work>/android/msgstore.db        decrypted
    <work>/ios_backup/<UDID>/…        unencrypted iPhone backup (this is what gets modified)
    <work>/ios_backup_pristine/…      untouched copy, so a failed restore can be retried
    <work>/convert/ChatStorage.sqlite modified WhatsApp database
    <work>/convert/report.json        import statistics + media jobs
    <work>/convert/thumbs/…           generated `.thumb` files
    <work>/ios_backup_scoped/<UDID>/… only the files that changed: this is what `ios restore` sends
"""

from __future__ import annotations

import dataclasses
import filecmp
import json
import logging
import os
import pathlib
import plistlib
import shutil
import sqlite3
import time
from collections.abc import Callable

from .android import adb, crypt15, msgstore
from .ios import backup as iosbackup
from .ios import chatstorage, device
from .reporting import emit_data, progress_callback

log = logging.getLogger(__name__)
Log = Callable[[str], None]


class PipelineError(Exception):
    pass


class Work:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.state_path = os.path.join(self.root, "state.json")
        self.state: dict = {}
        if os.path.isfile(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                self.state = json.load(fh)

    def save(self, **kw) -> None:
        self.state.update(kw)
        self.state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    # convenience accessors
    @property
    def android_dir(self) -> str:
        return self.path("android")

    @property
    def msgstore_db(self) -> str:
        return self.path("android", "msgstore.db")

    @property
    def media_dir(self) -> str | None:
        p = self.path("android", "Media")
        return p if os.path.isdir(p) else None

    @property
    def backup_root(self) -> str:
        return self.path("ios_backup")

    @property
    def convert_dir(self) -> str:
        return self.path("convert")


# ------------------------------------------------------------------------------ stages

def android_pull(work: Work, *, serial: str | None = None, include_media: bool = True,
                 business: bool = False, out: Log = print) -> dict:
    out("→ Looking for the Android phone …")
    dev = adb.require_device(serial)
    out(f"  found {dev.model or dev.serial}")
    summary = adb.pull_whatsapp(work.android_dir, serial=dev.serial, include_media=include_media,
                                business=business, progress=lambda line: out("  " + line))
    summary["business"] = business
    contacts = adb.export_contacts(dev.serial)
    if contacts:
        with open(work.path("android", "contacts.json"), "w", encoding="utf-8") as fh:
            json.dump(contacts, fh)
        out(f"  exported {len(contacts)} contact numbers for name resolution")
    else:
        out("  (could not read contacts over ADB — pass --contacts <export.vcf> later for names)")
    work.save(android_pull=summary, android_serial=dev.serial)
    return summary


def newest_crypt15(databases_dir: str) -> str:
    cands = [os.path.join(databases_dir, f) for f in os.listdir(databases_dir) if f.endswith(".crypt15")]
    if not cands:
        if any(f.endswith(".crypt14") for f in os.listdir(databases_dir)):
            raise PipelineError(
                "Only .crypt14 backups here, and their key can't be read without root. In WhatsApp turn on "
                "Settings > Chats > Chat backup > End-to-end encrypted backup with the 64-digit key, then back up."
            )
        raise PipelineError(f"No .crypt15 file in {databases_dir}")
    # msgstore.db.crypt15 is the latest; dated files are older snapshots
    plain = [c for c in cands if os.path.basename(c) == "msgstore.db.crypt15"]
    return plain[0] if plain else max(cands, key=os.path.getmtime)


def android_decrypt(work: Work, key: str, *, crypt_file: str | None = None, force: bool = False,
                    out: Log = print) -> str:
    src = crypt_file or newest_crypt15(work.path("android", "Databases"))
    out(f"→ Decrypting {os.path.basename(src)} …")
    try:
        crypt15.decrypt_file(src, work.msgstore_db, key, force=force)
    except crypt15.Crypt15Error as e:
        out(f"  built-in decryptor failed ({e}); trying wa-crypt-tools if installed …")
        if not crypt15.wadecrypt_fallback(src, work.msgstore_db, key):
            raise
    size = os.path.getsize(work.msgstore_db)
    out(f"  OK → {work.msgstore_db} ({size/1e6:.1f} MB)")
    work.save(android_decrypt={"source": src, "output": work.msgstore_db})
    return work.msgstore_db


def load_archive(work: Work, *, contacts_vcf: str | None = None, include_system: bool = False,
                 skip_archived: bool = False):
    contacts: dict[str, str] = {}
    cj = work.path("android", "contacts.json")
    if os.path.isfile(cj):
        with open(cj, encoding="utf-8") as fh:
            contacts.update(json.load(fh))
    if contacts_vcf:
        contacts.update(msgstore.load_vcf(contacts_vcf))
    return msgstore.parse(work.msgstore_db, contacts=contacts, include_system=include_system,
                          include_revoked=True, skip_archived=skip_archived)


def _is_business(work: Work) -> bool:
    return work.state.get("android_pull", {}).get("business", False)


def _domain(work: Work) -> str:
    return iosbackup.WA_BUSINESS_GROUP_DOMAIN if _is_business(work) else iosbackup.WA_GROUP_DOMAIN


def _check_inside_work(work: Work, folder: str) -> None:
    """Refuse to modify a backup that is not our own copy (e.g. the user's real Finder backup)."""
    f = os.path.abspath(folder)
    if os.path.commonpath([f, work.root]) != work.root:
        raise PipelineError(
            f"{folder} is outside the work directory. WaBridge only modifies backups it created itself "
            f"(copy the folder into {work.backup_root} if you really want to use it)."
        )


def ios_backup(work: Work, *, udid: str | None = None, out: Log = print) -> str:
    out("→ Backing up the iPhone (this can take a while; keep it unlocked and plugged in) …")
    out(f"  free disk space here: {free_bytes(work.root) / 1e9:.1f} GB "
        "(a full iPhone backup usually needs roughly the iPhone's used storage)")
    prog = progress_callback(out, "ios_backup")

    # A previous (possibly already modified) backup must not be incrementally updated.
    prev = work.state.get("ios_backup", {}).get("folder")
    if prev and os.path.isdir(prev):
        shutil.rmtree(prev)
    folder = device.backup(work.backup_root, udid=udid, progress=prog)

    # Check for WhatsApp BEFORE making the pristine copy: a copy taken while WhatsApp was not yet
    # installed can never be converted from, and it would otherwise be kept forever below.
    domain = _domain(work)
    app = "WhatsApp Business" if _is_business(work) else "WhatsApp"
    with iosbackup.Backup(folder, read_only=True) as b:
        if not b.has_whatsapp(domain):
            raise PipelineError(
                f"{app} data is not in this backup. Install {app} on the iPhone, register the SAME "
                "phone number, send one message to anyone, then run `wabridge ios backup` again."
            )

    # Keep the FIRST backup of this phone untouched forever: it is the rollback point.
    pristine = os.path.join(work.path("ios_backup_pristine"), os.path.basename(folder))
    if os.path.isdir(pristine):
        usable = os.path.isfile(os.path.join(pristine, "Manifest.db"))
        if usable:
            with iosbackup.Backup(pristine, read_only=True) as pb:
                usable = pb.has_whatsapp(domain)
        if usable:
            out(f"  keeping existing pristine copy at {pristine}")
        else:
            # Left behind by an interrupted copy or by a build that copied before checking for WhatsApp.
            shutil.rmtree(pristine, ignore_errors=True)
            _clone_tree(folder, pristine)
            out(f"  replaced an unusable pristine copy at {pristine}")
    else:
        _clone_tree(folder, pristine)
        out(f"  pristine copy saved to {pristine}")

    out(f"  OK → {folder}")
    work.save(ios_backup={"folder": folder, "pristine": pristine})
    return folder


def _clone_tree(src: str, dst: str) -> None:
    """Copy a directory; on macOS/APFS use clonefile (`cp -c`) so it costs ~no disk space."""
    import platform
    import subprocess

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if platform.system() == "Darwin":
        r = subprocess.run(["cp", "-Rc", src, dst], capture_output=True, text=True)
        if r.returncode == 0:
            return
        shutil.rmtree(dst, ignore_errors=True)
    try:
        shutil.copytree(src, dst)
    except OSError as e:
        # Never leave a half-copied pristine behind: a later run would trust it as the rollback point.
        shutil.rmtree(dst, ignore_errors=True)
        raise PipelineError(
            f"Could not make the pristine copy of the backup ({e.strerror or e}). On this file system the copy "
            "needs as much free space again as the backup itself; free up space or move the work folder."
        ) from e


def free_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def ios_rollback(work: Work, *, udid: str | None = None, system: bool = False, out: Log = print) -> None:
    """Restore the untouched pre-migration backup to the iPhone."""
    pristine = work.state.get("ios_backup", {}).get("pristine")
    if not pristine or not os.path.isfile(os.path.join(pristine, "Manifest.db")):
        raise PipelineError("No pristine backup recorded in this work directory")
    with iosbackup.Backup(pristine, read_only=True) as b:
        if b.missing_regular_chats():
            raise PipelineError("This backup doesn't contain regular WhatsApp's chats, and a rollback restores the "
                                "whole phone, so it could lose them. Not rolling back.")
    out(f"→ Restoring the PRISTINE backup ({pristine}) to the iPhone …")
    device.restore(os.path.dirname(pristine), udid=udid, system=system,
                   progress=progress_callback(out, "ios_rollback"))
    work.save(ios_rollback={"done": True})


def convert(work: Work, *, backup_folder: str | None = None, contacts_vcf: str | None = None,
            include_media: bool = True, include_system: bool = False, skip_archived: bool = False,
            out: Log = print) -> chatstorage.ImportReport:
    folder = backup_folder or work.state.get("ios_backup", {}).get("folder") or \
        iosbackup.find_backup_dir(work.backup_root)
    _check_inside_work(work, folder)
    domain = _domain(work)
    out("→ Reading Android database …")
    archive = load_archive(work, contacts_vcf=contacts_vcf, include_system=include_system,
                           skip_archived=skip_archived)
    st = archive.stats()
    out(f"  {st['chats']} chats ({st['groups']} groups), {st['messages']} messages, {st['media']} media")
    emit_data(out, "archive_stats", st)

    # Always start from the untouched database so a re-run (e.g. the media pass after a
    # text-only pass) rebuilds everything instead of deduplicating against our own output.
    pristine = work.state.get("ios_backup", {}).get("pristine")
    source = pristine if pristine and os.path.isfile(os.path.join(pristine, "Manifest.db")) else folder
    out(f"→ Extracting ChatStorage.sqlite from the {'pristine' if source == pristine else 'iPhone'} backup …")
    os.makedirs(work.convert_dir, exist_ok=True)
    for stale in ("ChatStorage.sqlite", "ChatStorage.sqlite-wal", "ChatStorage.sqlite-shm"):
        p = os.path.join(work.convert_dir, stale)
        if os.path.exists(p):
            os.remove(p)
    with iosbackup.Backup(source, read_only=True) as b:
        db = b.extract_chatstorage(work.convert_dir, domain=domain)
        archive.owner_jid = b.owner_jid(domain)

    out("→ Writing messages into ChatStorage.sqlite …")
    w = chatstorage.Writer(db)
    if not w.message_count():
        # The writer copies its conventions from WhatsApp's own rows; with none it would be guessing.
        raise PipelineError(
            "WhatsApp hasn't written any messages on the iPhone yet. Send and receive a message there, "
            "then back up the iPhone again."
        )
    thumbs = work.path("convert", "thumbs")
    shutil.rmtree(thumbs, ignore_errors=True)
    rep = w.import_archive(archive, media_root=work.media_dir if include_media else None,
                           include_media=include_media, thumb_dir=thumbs)
    w.finalize()
    media_note = (f"media: {rep.media_linked} linked, {rep.media_missing} shown without a file"
                  if include_media else "media: skipped (--no-media)")
    out(f"  sessions: {rep.sessions_created} new, {rep.sessions_reused} existing; "
        f"messages: {rep.messages_written} written, {rep.messages_skipped_duplicate} already present; "
        + media_note)
    out(f"  replies: {rep.replies}, reactions: {rep.reactions}, deleted markers: {rep.revoked}, "
        f"thumbnails: {rep.thumbnails}, group events skipped: {rep.system_skipped}")
    if rep.replies_without_sender:
        out(f"  note: {rep.replies_without_sender} replies have no sender: the iPhone backup doesn't say whose it is")
    with open(work.path("convert", "report.json"), "w", encoding="utf-8") as fh:
        json.dump(dataclasses.asdict(rep), fh, indent=2)
    emit_data(out, "convert_report", {k: v for k, v in dataclasses.asdict(rep).items() if k != "media_jobs"})
    work.save(convert={"chatstorage": db, "backup_folder": folder, "domain": domain,
                       "messages": rep.messages_written, "media_jobs": len(rep.media_jobs)}, verify={})
    return rep


def inject(work: Work, *, backup_folder: str | None = None, out: Log = print) -> None:
    folder = backup_folder or work.state.get("convert", {}).get("backup_folder") or \
        iosbackup.find_backup_dir(work.backup_root)
    _check_inside_work(work, folder)
    domain = work.state.get("convert", {}).get("domain") or _domain(work)
    rp = work.path("convert", "report.json")
    if not os.path.isfile(rp):
        raise PipelineError("Run `wabridge convert` first")
    with open(rp, encoding="utf-8") as fh:
        report = json.load(fh)
    db = work.path("convert", "ChatStorage.sqlite")
    out("→ Adding the chats (and any media) to the iPhone backup …")
    with iosbackup.Backup(folder) as b:     # rolls back Manifest.db if anything below raises
        b.put(domain, iosbackup.CHATSTORAGE, db)
        for suffix in ("-wal", "-shm"):
            b.remove(domain, iosbackup.CHATSTORAGE + suffix)
        jobs = report.get("media_jobs", [])
        prog = progress_callback(out, "inject")
        for i, job in enumerate(jobs, 1):
            b.put(domain, job["relative_path"], job["local_file"],
                  mtime=int(os.path.getmtime(job["local_file"])))
            if i % 50 == 0 or i == len(jobs):
                prog(100.0 * i / len(jobs))
        b.touch_status()
    out("  OK")
    work.save(inject={"backup_folder": folder, "media": len(report.get("media_jobs", []))})
    if work.state.get("ios_backup", {}).get("pristine"):
        verify(work, out=out)


def verify(work: Work, *, out: Log = print) -> dict:
    """Prove the modified backup differs from the untouched one only where it should: WhatsApp's database and
    new media in the target app. Every other file on the phone, including the other app's chats, must be
    byte-identical, because a restore writes all of them back."""
    st = work.state
    folder, pristine = st.get("inject", {}).get("backup_folder"), st.get("ios_backup", {}).get("pristine")
    domain = st.get("convert", {}).get("domain")
    if not folder or not pristine or not domain:
        raise PipelineError("Nothing to verify yet: run `wabridge inject` first")
    out("→ Comparing the modified backup with the untouched one …")
    work.save(verify={})
    with iosbackup.Backup(pristine, read_only=True) as a, iosbackup.Backup(folder, read_only=True) as b:
        query = "SELECT fileID, domain, relativePath, flags, file FROM Files"
        before = {r["fileID"]: r for r in a.conn.execute(query)}
        after = {r["fileID"]: r for r in b.conn.execute(query)}
        problems: list[str] = []
        store = iosbackup.CHATSTORAGE
        same = [fid for fid, r in before.items() if fid in after and tuple(r) == tuple(after[fid])]
        for fid, r in before.items():
            if fid not in after:
                if (r["domain"], r["relativePath"]) not in ((domain, store + "-wal"), (domain, store + "-shm")):
                    problems.append(f"removed: {r['domain']}/{r['relativePath']}")
            elif tuple(r) != tuple(after[fid]) and (r["domain"], r["relativePath"]) != (domain, store):
                problems.append(f"changed: {r['domain']}/{r['relativePath']}")
        added = [r for fid, r in after.items() if fid not in before]
        problems += [f"added outside Message/: {r['domain']}/{r['relativePath']}" for r in added
                     if r["domain"] != domain or not r["relativePath"].startswith("Message")]
        prog = progress_callback(out, "verify")
        for i, fid in enumerate(same, 1):
            if before[fid]["flags"] == iosbackup.FLAG_FILE:
                pa, pb = a.blob_path(fid), b.blob_path(fid)
                if os.path.isfile(pa) != os.path.isfile(pb) or (
                        os.path.isfile(pa) and not filecmp.cmp(pa, pb, shallow=False)):
                    problems.append(f"content differs: {before[fid]['domain']}/{before[fid]['relativePath']}")
            if i % 2000 == 0 or i == len(same):
                prog(100.0 * i / len(same))
        entry = b.get(domain, store)
        problems += _check_chatstorage(b.blob_path(entry.file_id) if entry else "")
    if problems:
        raise PipelineError(f"The modified backup failed verification, do NOT restore it. {len(problems)} problem(s), "
                            f"first: {'; '.join(problems[:5])}")
    wanted = {fid for fid, r in after.items() if fid not in before or tuple(r) != tuple(before[fid])}
    scoped = _build_scoped_backup(work, folder, domain, wanted)
    result = {"ok": True, "files_identical": len(same), "files_added": len(added), "scoped": scoped}
    out(f"  OK: {len(same)} files identical, {len(added)} added, nothing else changed")
    work.save(verify=result)
    return result


def _build_scoped_backup(work: Work, folder: str, domain: str, wanted: set[str], *, clear_wal: bool = True) -> str:
    """The restore payload: a partial backup holding only the files I changed, plus the folders above them.
    Other apps' data is not in it, and in one test on iOS 27.2 regular WhatsApp's chats survived. It is NOT a
    light operation: the iPhone rebooted into the setup assistant and lost its settings, home screen layout and
    other system data, and apps redownloaded after signing in. Empty -wal/-shm are included because a stale
    WAL next to my database would corrupt it (that part is unverified on a device)."""
    udid = os.path.basename(folder)
    root = work.path("ios_backup_scoped")
    shutil.rmtree(root, ignore_errors=True)
    dest = os.path.join(root, udid)
    os.makedirs(dest)
    for name in ("Manifest.db", "Manifest.plist", "Info.plist", "Status.plist"):
        if os.path.isfile(os.path.join(folder, name)):
            shutil.copyfile(os.path.join(folder, name), os.path.join(dest, name))
    info_path = os.path.join(dest, "Info.plist")
    if os.path.isfile(info_path):                          # its app list makes iOS reinstall apps after a restore
        with open(info_path, "rb") as fh:
            info = plistlib.load(fh)
        info.pop("Applications", None)
        with open(info_path, "wb") as fh:
            plistlib.dump(info, fh, fmt=plistlib.FMT_BINARY)
    conn = sqlite3.connect(os.path.join(dest, "Manifest.db"))
    conn.execute("PRAGMA journal_mode=DELETE")
    keep = set(wanted)
    rows = dict(conn.execute("SELECT relativePath, fileID FROM Files WHERE domain=?", (domain,)).fetchall())
    paths = {rel: fid for rel, fid in rows.items() if fid in wanted}
    for rel in paths:           # the folders above each file, up to the app's root (""): iOS needs them to place it
        parts = rel.split("/")[:-1]
        keep.update(rows[p] for p in ("/".join(parts[:i]) for i in range(len(parts) + 1)) if p in rows)
    conn.execute("CREATE TEMP TABLE keep(fileID TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO keep VALUES (?)", [(fid,) for fid in keep])
    conn.execute("DELETE FROM Files WHERE fileID NOT IN (SELECT fileID FROM keep)")
    conn.commit()
    files = [r[0] for r in conn.execute("SELECT fileID FROM Files WHERE flags=?", (iosbackup.FLAG_FILE,))]
    conn.close()
    for fid in files:
        src, dst = os.path.join(folder, fid[:2], fid), os.path.join(dest, fid[:2], fid)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    if clear_wal:
        empty = work.path("convert", "empty")
        open(empty, "wb").close()
        with iosbackup.Backup(dest) as b:
            for suffix in ("-wal", "-shm"):
                b.put(domain, iosbackup.CHATSTORAGE + suffix, empty)
    return dest


def _check_chatstorage(path: str) -> list[str]:
    if not os.path.isfile(path):
        return ["ChatStorage.sqlite is missing"]
    conn = sqlite3.connect(f"{pathlib.Path(path).as_uri()}?immutable=1", uri=True)
    try:
        problems = []
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            problems.append(f"ChatStorage integrity_check: {result}")
        for table, entity in (("ZWAMESSAGE", "WAMessage"), ("ZWAMEDIAITEM", "WAMediaItem"),
                              ("ZWACHATSESSION", "WAChatSession")):
            row = conn.execute(f"SELECT (SELECT MAX(Z_PK) FROM {table}), Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME=?",
                               (entity,)).fetchone()
            if row and row[0] is not None and row[0] > (row[1] or 0):
                problems.append(f"{entity}: Z_MAX is below the highest row")
        return problems
    finally:
        conn.close()


def ios_restore(work: Work, *, udid: str | None = None, system: bool = False, out: Log = print) -> None:
    if not work.state.get("verify", {}).get("ok"):
        raise PipelineError("The modified backup hasn't passed verification. Run `wabridge verify` first.")
    root = work.path("ios_backup_scoped")
    out("→ Sending only the changed files to the iPhone …")
    out("  The phone will reboot. Do NOT unplug it until it shows the lock screen again.")
    device.restore(root, udid=udid, system=system, skip_apps=True, progress=progress_callback(out, "ios_restore"))
    out("  Restore command finished. Open WhatsApp on the iPhone once it has rebooted.")
    work.save(ios_restore={"done": True, "system": system})


def migrate(work: Work, *, key: str, serial: str | None = None, udid: str | None = None,
            include_media: bool = True, contacts_vcf: str | None = None, system: bool = False,
            business: bool = False, out: Log = print) -> None:
    android_pull(work, serial=serial, include_media=include_media, business=business, out=out)
    android_decrypt(work, key, out=out)
    folder = ios_backup(work, udid=udid, out=out)
    convert(work, backup_folder=folder, contacts_vcf=contacts_vcf, include_media=include_media, out=out)
    inject(work, backup_folder=folder, out=out)
    ios_restore(work, udid=udid, system=system, out=out)
