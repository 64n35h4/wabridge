# WaBridge

**Free, open-source WhatsApp migration from Android to iPhone — chats, groups and media, without factory-resetting the iPhone.**

The paid tools (Dr.Fone, MobileTrans, Mutsapper, Wazzap Migrator…) all do the same thing under the hood: decrypt the Android database, rewrite it into WhatsApp-for-iOS's `ChatStorage.sqlite`, slip it into an iPhone backup and restore that backup. Every one of those steps can be done with open-source software. WaBridge is that pipeline, automated, GPL-licensed, and free forever.

> **Status: alpha, but the full path has worked on real phones.** Text and group chats first worked on 2026-09-22 (Samsung Galaxy Z Fold4 → iPhone 13, iOS 27.2, regular WhatsApp). On 2026-11-02 a WhatsApp Business history of roughly 380,000 messages moved from a Pixel 8 Pro (Android 17) to an iPhone 18 Pro Max (iOS 27.2), and I confirmed chats, media, reactions, stickers and replies in the app. That is one run on each setup, not a guarantee. **Read [what a restore does to the iPhone](#what-a-restore-does-to-the-iphone) first, and always keep your Android backup.**

## How it works

```
Android phone ─adb pull─▶ msgstore.db.crypt15 + Media/   (no root needed)
                            │  your 64-digit backup key
                            ▼
                        msgstore.db ──parse──▶ neutral model ──write──▶ ChatStorage.sqlite
                                                                             │
iPhone ─backup─▶ unencrypted backup folder ─────────────── inject ◀──────────┘
                                                              │
iPhone ◀─────────────────────────── restore modified backup ──┘
```

1. On Android, WhatsApp's end-to-end encrypted backup gives you a **64-digit key**. With it, the local `msgstore.db.crypt15` decrypts without root.
2. On the iPhone, WhatsApp is installed and registered with the **same phone number**. A normal (unencrypted) backup of the iPhone is taken.
3. WaBridge converts the Android messages into WhatsApp-for-iOS's Core Data database inside that backup, adds the media files, and restores the backup. WhatsApp on the iPhone opens with your history.

The official free path, Apple's *Move to iOS*, also works but requires a **factory-reset** iPhone. WaBridge is for everyone who already set the iPhone up.

### What moves

| Item | State |
|---|---|
| Text and group chats | Confirmed on a phone |
| Contact names | Taken from the Android phone's contacts; a `.vcf` export also works |
| Photos, videos and other files still on the Android phone | Confirmed on a phone: they transfer and open |
| Media no longer on the Android phone | The bubble appears, without the file |
| Replies, reactions, stickers | Confirmed on a phone |
| Deleted-message markers, documents, voice notes | Written from WhatsApp's real row formats; not individually confirmed |
| Edited messages | The final text moves; the "Edited" label doesn't |
| Group events (joined, left, subject changed) | Not moved |
| WhatsApp Business to WhatsApp Business | Confirmed on a phone. Business labels and quick replies are not moved |

### What a restore does to the iPhone

WaBridge sends only the files it changed, for the one app it targets, and refuses to restore a backup that fails a byte-by-byte check against the untouched copy. Even so, **a restore is never harmless.** In one of my tests on iOS 27.2, a restore of a single file rebooted the iPhone into the setup assistant and reset system data: settings, home screen layout, app groupings and timezone. Find My came back on, and apps redownloaded after signing in to iCloud. Data inside other apps survived in that test, including another WhatsApp's chats, but nobody can promise that on your phone.

Before restoring: back up anything you can't lose, and expect to set the phone up again. Messages that arrive on the iPhone between the backup and the restore can be lost.

## Requirements

* Python 3.10+
* [Android platform-tools](https://developer.android.com/tools/releases/platform-tools) (`adb`) on your PATH
* USB debugging enabled on the Android phone
* iPhone: the same app (WhatsApp, or WhatsApp Business for a Business history) installed and activated with the same
  number, with at least one message; **"Encrypt local backup" turned off**; **Stolen Device Protection and Find My iPhone
  turned off** while restoring (Apple refuses the restore otherwise; turn them back on after checking the result)
* The Android backup must be end-to-end encrypted with the **64-digit key**. Without it, backups are `.crypt14`,
  which can't be read without root.
* Windows only: iTunes (or Apple Mobile Device Support) for the USB driver. Linux: `usbmuxd`. macOS: nothing extra.

## Desktop app

A guided app (Tauri + React shell around the same engine) lives in [`gui/`](gui/). It walks
through the five steps, watches for both phones, shows progress, and only stops when a tap on a
phone is needed. Build it with `bash build-app.sh` (Rust + Node 20 required) or run it in dev mode
with `bash build-app.sh dev`; installers for macOS, Windows and Linux are produced by the release
workflow on every `v*` tag. Status: it builds, opens and boots the engine on macOS (23 Sept 2026),
and its Android and iPhone steps have run with real phones (23 Sept 2026); the final restore
step through the app has not yet. Its WhatsApp Business choice hasn't been run with real phones either.
See [`gui/README.md`](gui/README.md).

### What it looks like

<table>
  <tr>
    <td width="50%"><img src="docs/img/start.png" alt="Start screen: three readiness checks and a Begin button" width="100%"></td>
    <td width="50%"><img src="docs/img/android-key.png" alt="Android screen: phone detected, 64-digit key field, folded instructions" width="100%"></td>
  </tr>
  <tr>
    <td><img src="docs/img/android-fetched.png" alt="Android screen after decrypting: chat, group, message and media counts" width="100%"></td>
    <td><img src="docs/img/iphone-backup.png" alt="iPhone screen while the backup runs, progress in the rail and the bar" width="100%"></td>
  </tr>
  <tr>
    <td><img src="docs/img/transfer.png" alt="Transfer screen: Find My iPhone warning and confirmation before moving the chats" width="100%"></td>
    <td><img src="docs/img/done.png" alt="Done screen: turn Find My back on, grant Contacts access, delete WaBridge's copies" width="100%"></td>
  </tr>
</table>

Rendered from the app's built-in mock engine (`cd gui && npm run screenshots`), so the phone
names, chat names and counts are invented; the layout and copy are the real app. A dark-scheme
version follows the system setting ([example](docs/img/start-dark.png)).

## Quick start in the terminal (no setup knowledge needed)

Download/unzip, open a terminal in the folder, and run **one command**:

```bash
./start.sh          # macOS / Linux
start.bat           # Windows (install iTunes from the Microsoft Store first, for the USB driver)
```

It installs Python, adb and the tool into the folder without admin rights, then launches
`wabridge wizard`, which does everything automatically and only stops when you must tap
something on a phone (allow USB debugging, copy the 64-digit key, tap Trust, …). Ctrl-C and
re-run any time; it resumes where it stopped. A full log lands in `wabridge-work/wizard.log`.

## Manual usage

```bash
pip install "wabridge[ios]"        # once published; until then: pip install -e ".[ios]"
wabridge doctor                     # checks adb, pymobiledevice3, connected devices
```

The one-shot command:

```bash
wabridge migrate --key "1234 5678 …(64 digits)…"              # add --business for WhatsApp Business
```

Or step by step (each step is resumable; everything lands in `./wabridge-work/`):

```bash
wabridge android pull                  # Databases/ + Media/ over ADB
wabridge android decrypt --key …       # -> msgstore.db
wabridge android inspect               # sanity check: chats & message counts
wabridge ios backup                    # full unencrypted iPhone backup
wabridge convert [--contacts my.vcf]   # write chats into the backup's ChatStorage.sqlite
wabridge inject                        # ChatStorage.sqlite + media into the backup, then verifies it
wabridge ios restore [--system]        # send only the changed files to the iPhone (it will reboot; see the warning above)
wabridge ios rollback                  # undo: a whole-phone restore of the untouched pre-migration backup
```

Step-by-step instructions for non-technical users, including how to find the 64-digit key, are in [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## Project layout

```
src/wabridge/
  model.py              platform-neutral Chat / Message / Media dataclasses
  android/adb.py        find device, pull WhatsApp folder, export contacts
  android/crypt15.py    .crypt15 decryption (HKDF + AES-256-GCM + zlib), no external tool
  android/msgstore.py   new-schema msgstore.db parser, LID -> phone mapping
  ios/backup.py         Manifest.db reader/writer, MBFile blobs, media injection
  ios/chatstorage.py    ChatStorage.sqlite writer (Core Data via plain SQL)
  ios/device.py         pymobiledevice3 wrapper (backup / restore / encryption)
  ios/probe.py          read-only structure report on a backup, no message text
  pipeline.py           resumable stages + `migrate`
  wizard.py             guided interactive terminal session (polls devices, resumes, logs)
  serve.py              JSON-lines engine for the desktop app (`wabridge serve`)
  reporting.py          structured log/progress shared by CLI, wizard and GUI
  cli.py
gui/                    Tauri 2 + React desktop app (engine runs as a sidecar)
start.sh / start.bat    zero-setup terminal launchers (uv + Python 3.12 + adb, no admin)
build-app.sh            build or dev-run the desktop app
tests/                  synthetic fixtures + 59 tests, no devices required
DESIGN.md               architecture, schema mapping, risks, roadmap
```

Run the tests: `python -m unittest discover tests` (or `pytest`).

## Roadmap

- [x] Engine: decrypt, parse, convert, inject
- [x] **Text + group chats migrate on real devices** (verified 2026-09-22)
- [x] **Media files open on the iPhone** (verified 2026-11-02). Files still on the phone are linked; the rest show as bubbles with no file
- [x] Replies/quotes, reactions, stickers (verified 2026-11-02)
- [ ] Deleted-message markers, documents, voice notes: written from real row formats, not individually confirmed
- [ ] "Edited" label (the edited text itself migrates)
- [ ] Group event messages (joined/left/subject changed) — not written yet
- [x] WhatsApp Business to WhatsApp Business (verified 2026-11-02 by running the engine stages)
- [ ] WhatsApp Business through the wizard, the desktop app and `migrate --business`: not yet run with phones
- [ ] Legacy (pre-2022) Android schema
- [x] Desktop GUI (Tauri shell around this engine): builds and boots on macOS; Android and iPhone steps ran with real phones
- [ ] The desktop app's Transfer step with real phones
- [ ] iOS → Android (the model layer is already direction-agnostic)

## Prior art and thanks

WaBridge stands on years of reverse-engineering by others: [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) (crypt15 format), [watoi](https://github.com/residentsummer/watoi) and [mwatoi](https://github.com/mukulkadel/mwatoi) (ChatStorage import), [WhatsApp-Chat-Exporter](https://github.com/KnugiHK/WhatsApp-Chat-Exporter) (both schemas), [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) (iOS backup protocol), and the forensic community's schema documentation.

## Licence

GPL-3.0-or-later. That is deliberate: anyone may use, study and improve WaBridge, and nobody may take it closed-source and sell it back to you.

WhatsApp is a trademark of Meta Platforms, Inc. This project is not affiliated with or endorsed by Meta or Apple.
