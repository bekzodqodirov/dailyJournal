# MIYA Call Companion (Android)

A sideloaded, single-owner Android app that **watches the folder your phone's own
dialer writes call recordings into** and uploads each finished recording, plus
whatever call metadata the phone can honestly supply, to your self-hosted MIYA
server.

It is the phone half of the design in the architecture document. The server half
(`POST /v1/recordings`, `POST /v1/recordings/probe`) is built separately; the
wire contract below is implemented here exactly as specified.

---

## Read this first: what this app cannot do

**MIYA never records a call, and no app you install on an unrooted phone can.**
This is not a policy we chose to respect — it is enforced in the Android audio
framework:

- `VOICE_CALL` / `VOICE_UPLINK` / `VOICE_DOWNLINK` and capture from
  `TYPE_TELEPHONY` need `CAPTURE_AUDIO_OUTPUT`, which is `signature|privileged`.
  A sideloaded app cannot get it — not from the user, not via `adb pm grant`,
  not as a Device Owner.
- `MIC` / `VOICE_COMMUNICATION` open fine with plain `RECORD_AUDIO`, but while
  `AudioManager.getMode()` is `MODE_IN_CALL` or `MODE_IN_COMMUNICATION` the
  framework **silences** an ordinary app's capture stream. No exception is
  thrown. You get a valid recorder, a valid `.m4a`, and zeros inside it. That
  silent file is the single most common false positive in this space — "it works
  on my phone" almost always means "a file was produced", not "audio was
  captured".
- `CallScreeningService` gives you the number, not audio. `InCallService` /
  `ROLE_DIALER` gives call state and audio *routing*, never audio *samples*.
  UICC carrier privileges have no audio API. `DevicePolicyManager` has no
  call-audio API.

**So this app does not request `RECORD_AUDIO` and contains no recording code.**
It reads what the OEM dialer already wrote.

The hard consequence, surfaced in onboarding rather than discovered in
production:

1. **If your phone has no native call recorder, this app cannot capture audio on
   it.** No permission, no code path, no cleverness fixes that. Native recording
   is absent on Samsung across the EU, absent on Xiaomi Global/EEA HyperOS
   builds, and CSC-gated even within one country on Samsung.
2. **On a Pixel — or any phone running Google Dialer — recordings live in
   `/data/user/0/com.google.android.dialer/files/callrecording`**, which is
   unreachable by `MANAGE_EXTERNAL_STORAGE`, unreachable by SAF, and refused by
   DocumentsUI even as `EXTRA_INITIAL_URI`. The app detects this and switches to
   **share-only mode**: you share each recording out of the Phone app into MIYA
   by hand.
3. **It cannot capture WhatsApp, Telegram or any VoIP call.** The OEM dialer only
   sees cellular calls.
4. **It cannot stop your dialer announcing "this call is being recorded"** to the
   other person. Samsung (US) and Google do this. It is your dialer's behaviour,
   not ours — but users report it as a bug in *our* app, so it is documented here
   and in the app.
5. **It cannot guarantee a recording is uploaded within N seconds.** Best case is
   roughly one to two minutes. Under Doze, on a killed process, or offline it can
   be hours; on an aggressive MIUI build with the autostart toggles reset it can
   be *until you next open the app*. The queue guarantees eventual delivery.
   Nothing guarantees timeliness.
6. **It cannot survive an OEM cleaner deleting the source file first.** MIUI
   Cleaner and Samsung Device Care delete old call recordings without warning.
   The app ingests on call-end for exactly this reason, but a file deleted before
   the phone comes back online is gone.
7. **It cannot recover call direction on a phone where `READ_CALL_LOG` is
   ungrantable.** No filename heuristic has ever encoded who called whom. Those
   calls are uploaded with `direction: "unknown"`.
8. **It cannot be shipped on Google Play,** and from September 2026 sideloading
   itself needs a registered developer identity (Android Developer Verifier:
   enforcement starts in BR/ID/SG/TH, global target 2027). Register the package
   name and signing certificate before then.
9. **It does not make your API safe to expose publicly.** It assumes a private
   overlay network (Tailscale/WireGuard). A static bearer token on the open
   internet, in front of every debt, transcript and contact you have, is not an
   acceptable posture and TLS does not make it one.
10. **It does not address consent.** Recording both sides of a call is legally
    constrained in many jurisdictions. That is your decision to make knowingly.

---

## Android version range

| | |
|---|---|
| **Minimum** | **Android 10 (API 29)** |
| **Target / compile** | **Android 16 (API 36)** |

Android 10 is a hard floor, not a preference:

- the multi-directory `FileObserver(List<File>, int)` constructor arrived in
  API 29,
- `MediaStore.RELATIVE_PATH` (how folders are detected, because `File.exists()`
  lies under scoped storage) arrived in API 29,
- `ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC` arrived in API 29,
- and `java.time` is available natively from API 26, so no desugaring is needed
  for the ISO-8601-with-offset timestamps the server contract requires.

Behaviour that changes across the supported range is handled explicitly:

| Version | What changes | How it is handled |
|---|---|---|
| 10 (29) | `MANAGE_EXTERNAL_STORAGE` and `MediaStore.getGeneration()` do not exist | Guarded by `Build.VERSION` in `StorageAccess` / `MediaStoreQuery` |
| 11 (30) | Scoped storage walls; `/Android/data` unreachable by anything | Google-Dialer phones detected and reported as unreachable |
| 12 (31) | `TelephonyCallback` replaces `PhoneStateListener`; background FGS starts restricted | Both call-state paths implemented; FGS start failure falls back to WorkManager |
| 13 (33) | `READ_MEDIA_AUDIO`, `POST_NOTIFICATIONS` | Requested; `READ_EXTERNAL_STORAGE` capped at `maxSdkVersion="32"` |
| 14 (34) | FGS types mandatory; `Service.onTimeout(int)` | `dataSync` declared and passed to `startForeground()`; `onTimeout` overridden |
| 15 (35) | `dataSync` capped at 6 h per 24 h; cannot start `dataSync` FGS from `BOOT_COMPLETED` | Service runs in bursts of seconds and stops itself; `BootReceiver` uses WorkManager only |
| 16 (36) | Target level | No resident service, no accessibility service, no `RECORD_AUDIO` |

---

## How it works

```
OEM Dialer ──writes──▶ /Recordings/Call/*.m4a
                            │
        FileObserver(CLOSE_WRITE|MOVED_TO) ─┐
        MediaStore ContentObserver ─────────┤
        TelephonyCallback CALL_STATE_IDLE ──┤   (primary trigger)
        ScanWorker every 15 min ────────────┘   (reconciliation net)
                            ▼
                     RecordingScanner
                       · stability gate (size stable 2 s + container parses)
                       · streaming SHA-256
                       · CallLog correlation, else filename, else mtime
                       · Room row (PK = sha256)   ← durable queue
                            ▼
                     UploadWorker (WorkManager)
                       · POST /v1/recordings/probe   (cheap "do you have it?")
                       · POST /v1/recordings         (meta part, then audio part)
                       · exponential backoff, 30 s base, capped at 5 h
```

### Four independent dedupe layers

1. **Room primary key on `sha256`** — the same bytes can only occupy one queue
   row, no matter how many observers fire.
2. **A `(sourceUri, size, mtime)` index** — a file already hashed is never
   re-read on a later sweep.
3. **The `probe` endpoint** — a lost `202` costs one small JSON round trip, not
   40 MB over a 3G link.
4. **Server-side dedupe** on `sha256` *or* `call_id`, before a byte is spooled.

`200 duplicate` and `202 accepted` are **both treated as success**. Treating a
duplicate as a failure is how a flaky network turns into re-uploading your whole
call history every night.

### Never ingesting a half-written file

Hashing a partial file is the worst outcome available: it poisons dedupe with a
hash the finished file will never match, so the real recording gets ingested a
second time later *and* you pay to transcribe truncated audio. Four gates:

- listen for `CLOSE_WRITE` / `MOVED_TO` only — never `CREATE` or `MODIFY`
  (Samsung writes a `.3ga` *during* the call and produces the `.m4a` at the end);
- reject `.3ga`, `.tmp`, `.part`, dotfiles and unknown extensions by name;
- require the file size to be unchanged across ~2 s of polling;
- require `MediaMetadataRetriever` to return a positive duration — a truncated
  MP4 has no readable `moov` atom and fails here.

---

## The upload contract

`POST /v1/recordings` — `multipart/form-data`, **exactly two parts, `meta`
first** so the server can reject on metadata before spooling bytes:

```
--boundary
Content-Disposition: form-data; name="meta"
Content-Type: application/json

{ …see below… }
--boundary
Content-Disposition: form-data; name="audio"; filename="Call recording Akmal_250817_143025.m4a"
Content-Type: audio/mp4

<bytes, streamed>
--boundary--
```

Metadata object (schema 1). Required: `schema`, `device_id`, `sha256`,
`size_bytes`, `started_at`. Everything else may be `null`:

```jsonc
{
  "schema": 1,
  "device_id": "b7f1c2e0-…",           // stable UUID per install, never regenerated
  "call_id": "b7f1c2e0:4711",          // "<device_id>:<CallLog.Calls._ID>", null if no call log
  "sha256": "a1b2c3…",                 // lowercase hex of the bytes as uploaded
  "size_bytes": 4193280,
  "started_at": "2026-09-06T14:30:25+05:00",  // ISO-8601 WITH OFFSET
  "duration_seconds": 412,
  "direction": "incoming",             // incoming | outgoing | missed | rejected | unknown
  "counterparty_name": "Akmal aka",
  "phone_e164": "+998901234567",
  "locale": "uz",
  "sim_slot": 0,
  "mime": "audio/mp4",
  "original_filename": "Call recording Akmal_250817_143025.m4a",
  "recorded_by": "com.samsung.android.dialer",
  "correlation": "call_log",           // call_log | filename | mtime
  "app_version": "1.0.0",
  "client_ts": "2026-09-06T14:38:02+05:00"
}
```

`POST /v1/recordings/probe`:

```json
→ {"sha256": ["a1b2…"], "call_id": ["dev7:4711"]}
← {"known_sha256": ["a1b2…"], "known_call_id": ["dev7:4711"]}
```

Response handling (this is what the retry logic keys on):

| Status | App does |
|---|---|
| `202` accepted | mark `DONE`, drop from queue |
| `200` duplicate | mark `DONE`, drop from queue — **success, not an error** |
| `401` | stop all uploads, red banner in Health, no retry |
| `422` `sha256 mismatch` | re-hash locally, retry **once**, then park permanently |
| `422` anything else | `FAILED_PERMANENT`, shown in the Queue screen, no retry |
| `503` / `5xx` / timeout / reset | exponential backoff, 30 s base |

---

## Building it

Requirements: **Android Studio Ladybug (2024.2) or newer, and JDK 17.**

```bash
# from the repository root
cd android
# Android Studio: File → Open → select this "android" directory, then Build → Make Project
```

Command line, once a Gradle wrapper exists:

```bash
gradle wrapper --gradle-version 8.11.1   # only needed once; the wrapper JAR is not committed
./gradlew :app:assembleRelease
```

The wrapper **JAR is deliberately not in the repository** (it is a binary blob).
`gradle/wrapper/gradle-wrapper.properties` pins Gradle 8.11.1; Android Studio
generates the wrapper for you on first sync, or run `gradle wrapper` once with a
local Gradle 8.x.

Toolchain pinned in `build.gradle.kts`:

| | |
|---|---|
| Android Gradle Plugin | 8.9.1 |
| Gradle | 8.11.1 |
| Kotlin | 2.0.21 (+ `plugin.compose` 2.0.21) |
| KSP | 2.0.21-1.0.28 |
| Compose BOM | 2024.10.01 |
| Room | 2.6.1 |
| WorkManager | 2.9.1 |
| OkHttp | 4.12.0 |
| libphonenumber | 8.13.42 |

R8/minification is **off for release** on purpose: this app is sideloaded onto one
phone, and a stripped stack trace from an OEM-specific failure costs far more than
the few hundred kilobytes saved.

### Installing

```bash
# A plain install often leaves READ_CALL_LOG permanently ungrantable (it is a
# hard-restricted permission, allowlisted by the installer at install time, and
# a user cannot grant it by hand). The app is fully functional without it.
adb install -r app/release/app-release.apk

# If you want to try to keep the call-log permission grantable:
adb install -r --restrict-permissions app/release/app-release.apk
```

**Test the no-call-log path**, because on a sideloaded APK it may be the only one
that ever runs:

```bash
adb shell pm revoke uz.miya.companion android.permission.READ_CALL_LOG
```

Test the Android 15 foreground-service cap:

```bash
adb shell am compat enable FGS_INTRODUCE_TIME_LIMITS uz.miya.companion
```

---

## One-time setup on the phone

Ten to fifteen minutes, once. The app walks you through it and refuses to claim
it is working until each step is verified.

1. **On the VPS:** install Tailscale, note the tailnet IP, set
   `API_BIND=<tailnet-ip>`, add `UPLOAD_TOKENS=phone:<generated-token>` to `.env`,
   restart the API container.
2. **On the phone:** install Tailscale, sign in to the same tailnet.
3. **Turn on call recording in the OEM dialer.** Phone → Settings → "Record
   calls" (Samsung) or "Call recording" (Xiaomi/MIUI).
   *If the setting is not there, stop — no app can change that.*
4. **Make one 10-second test call and record it.** This is not busywork: on a
   fresh phone the recordings folder **does not exist until the first
   recording**, and `FileObserver` reports nothing for a directory that did not
   exist when it was registered — ever, even if it appears later.
5. **Install the APK** and open it.
6. **Grant** notifications, music & audio, phone state, contacts. The app also
   asks for the call log; it may be silently refused, and it carries on without
   it.
7. **Find the folder.** Tap *Auto-detect*; if that finds nothing, tap *Pick by
   hand* and choose the folder in the system picker (the picker opens pre-seeded
   at the most likely one).
8. **Disable battery optimisation** when prompted. This is load-bearing twice
   over: it relieves Doze deferral *and* it is one of the few exemptions that
   lets the app start its short foreground burst from the background.
9. **Work the OEM checklist** the app shows — Autostart, Background autostart,
   "No restrictions", removal from "Sleeping apps". The app deep-links each
   screen. No manifest entry can do any of this.
10. **Enter the server URL** (`http://<tailnet-ip>:8000`) and paste the bearer
    token, then tap **Test connection**. It hits `/health` *and*
    `/v1/recordings/probe` with an empty batch, so it proves reachability and
    auth separately — a green tunnel with a bad token otherwise looks identical
    to a working setup until the first real upload fails.
11. **Make one more test call.** Within about two minutes it should appear in the
    Queue screen as `sent`. If it does not, the Health screen names the failing
    step.

### Recurring, and unavoidable

**After every major OS update, re-open the app and re-check the Health screen.**
Samsung and Xiaomi reset the autostart and battery toggles on update, and there
is no API that notices this for us. The Health screen re-verifies on every
resume for exactly this reason — checking once at onboarding is the classic
mistake.

---

## Screens

- **Health** — the permanent honesty panel: last accepted upload, queue depth,
  failure count, last scan, last error verbatim, whether the call-end trigger is
  live, which folders are actually being watched, and every prerequisite with a
  green/amber/red state. Amber means "works without it, with less information" —
  `READ_CALL_LOG` and the phone-state trigger are amber, never red.
- **Queue** — every row with its state, correlation method, attempt count and
  **the exact `detail` string the server returned**. Paraphrasing a server error
  is how a fixable configuration problem becomes a mystery.
- **Settings** — server URL, bearer token (masked; encrypted with an Android
  KeyStore AES-GCM key, never in plain DataStore, never logged), folder
  re-detect/re-pick, all-files opt-in, Wi-Fi-only, delete-local-after-upload
  (default **off**), language hint, minimum call duration.

---

## Permissions, and why each one

| Permission | Why |
|---|---|
| `INTERNET`, `ACCESS_NETWORK_STATE` | Upload. |
| `READ_MEDIA_AUDIO` (33+) / `READ_EXTERNAL_STORAGE` (≤32) | Read the files the dialer wrote. Android 14's partial media access is visual-only, so audio stays all-or-nothing — which here is good news. |
| `READ_PHONE_STATE` | `CALL_STATE_IDLE` → the cheapest reliable "go look now" trigger. |
| `READ_CALL_LOG` | Direction, exact start time, duration, contact name. **Hard-restricted; often ungrantable on a sideload.** The app degrades to filename + mtime. |
| `READ_CONTACTS` | A display name for a number the call log did not cache. |
| `POST_NOTIFICATIONS` | Without it the foreground-service notification is suppressed, which makes the app invisible and much likelier to be killed. |
| `RECEIVE_BOOT_COMPLETED` | Re-arm observers after a reboot. |
| `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC` | `dataSync` is the honest type — its documented use cases are literally "local file processing" and "transfer data between a device and the cloud". `microphone` is deliberately **not** declared: we do not capture, and on Android 14+ it is a while-in-use type that cannot be started from the background at all. |
| `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | Doze relief **and** background FGS start. |
| `MANAGE_EXTERNAL_STORAGE` | Opt-in "advanced mode" only, never required to launch. It still cannot reach `/Android/data`, so it does not rescue Google Dialer recordings. |
| ~~`RECORD_AUDIO`~~ | **Never requested.** It would buy nothing and would make the app look exactly like the spyware it is not. |

---

## Folder candidates

Only two entries are safe to hardcode; everything else is discovered at runtime
and **confirmed by finding an actual audio file**. The path lists circulating on
GitHub contradict each other — the same repo family attributes
`/Recordings/Call/` to Samsung, Pixel *and* Huawei.

| Brand | Ordered candidates |
|---|---|
| Xiaomi / Redmi / POCO | `MIUI/sound_recorder/call_rec/` ← first-party confirmed · `MIUI/sound_recorder/` · `Recordings/Calls/` · `Recordings/` · `Recorder/call/` |
| Samsung | `Recordings/Call/` (One UI 5+) · `Call/` · `Sounds/` |
| Huawei / Honor | `Sounds/CallRecord/` ← widely corroborated |
| Oppo / OnePlus | `Music/Recordings/Call Recordings/` · `Recordings/Call Recordings/` · `PhoneRecord/` · `Record/Call/` |
| Vivo / iQOO | `Record/Call/` · `Sounds/CallRecord/` |
| Realme | `Recordings/` · `Music/Recordings/Call Recordings/` · `Calls/` — **or Google Dialer, unreachable** |
| Transsion (Tecno/Infinix/itel) | `PhoneRecord/` · `Music/PhoneRecord/` |
| tail (everyone) | `CallRecordings/` · `Call Recordings/` · `callrecordings/` · `Recorder/call/` · `Truecaller/` |
| **Google Dialer / Pixel** | **unreachable — share-only mode** |

Paths are resolved against `Environment.getExternalStorageDirectory()` and
MediaStore volume names, never a hardcoded `/storage/emulated/0` — under a work
profile or secondary user the id is not `0`.

Filename parsing is a **fallback only**, and it never matches on English
literals: "Call recording" is localized, so parsing anchors on digit runs and the
extension. The counterparty in a filename is a *contact name* when the number is
in contacts and a raw number when it is not, so the call log is always preferred.

---

## Source layout

```
app/src/main/java/uz/miya/companion/
  MiyaApp.kt                  Application; owns the FileObserver strong reference
  Graph.kt                    hand-rolled service locator
  data/
    Prefs.kt                  DataStore: server URL, folder, device_id, flags
    TokenStore.kt             bearer token, AES-GCM via Android KeyStore
    AppDatabase.kt            Room, version 1
    UploadEntity.kt           one row per recording; PK = sha256
    UploadDao.kt              insertIgnore, nextPending, observable queue
    RecordingRepository.kt    the only writer of queue state
    RecordingRef.kt           file / MediaStore row / SAF doc, unified
  discover/
    OemCandidates.kt          manufacturer-ordered candidate paths (a hint, not truth)
    MediaStoreQuery.kt        RELATIVE_PATH queries + generation deltas
    FolderProbe.kt            Found / GoogleDialerUnreachable / NoRecorder
    SafScanner.kt             picker intent, persisted grant, tree enumeration
  watch/
    RecordingFileObserver.kt  CLOSE_WRITE | MOVED_TO, multi-directory
    MediaStoreWatcher.kt      ContentObserver, notifyForDescendants = true
    CallStateWatcher.kt       TelephonyCallback (31+) / PhoneStateListener
    WatchArmer.kt             the one place that re-registers everything
    BootReceiver.kt           BOOT_COMPLETED / MY_PACKAGE_REPLACED
  service/
    IngestService.kt          short dataSync FGS burst, with onTimeout handling
  ingest/
    StabilityGate.kt          name + size + container gates
    Hasher.kt                 streaming SHA-256, 1 MiB buffer
    CallLogMatcher.kt         time-window correlation, degrades to null
    FilenameParser.kt         digit-anchored, never English-literal
    PhoneNormalizer.kt        libphonenumber → E.164, last-9-digit fallback
    RecordingScanner.kt       one scan, end to end
  net/
    MetaJson.kt               the §2.5 metadata object, literally
    MiyaClient.kt             OkHttp, auth interceptor, no call timeout
    StreamingRequestBody.kt   streams from ContentResolver, never readBytes()
    UploadApi.kt              probe + upload, status → UploadOutcome
  work/
    Scheduling.kt             periodic / burst / immediate / drain
    ScanWorker.kt             15-min reconciliation + "we have gone quiet" alarm
    UploadWorker.kt           one attempt per run, maps §2.7 exactly
    DrainWorker.kt            re-arms pending rows after reboot/force-stop
  ui/                         Compose: Onboarding, Health, Queue, Settings, share target
  oem/OemHints.kt             per-manufacturer deep links (dontkillmyapp.com)
  util/                       Notifications, StorageAccess, TimeFmt, Logx
```

---

## Known assumptions

Documented rather than hidden, because this project was written without an
Android SDK available to compile against:

- Dependency versions above are pinned to releases believed to exist and to be
  mutually compatible. If Gradle cannot resolve one, bump it — nothing in the
  code depends on a version-specific API.
- `compileSdk = 36` is newer than the level AGP 8.9.1 was tested against;
  `android.suppressUnsupportedCompileSdk=36` in `gradle.properties` silences the
  advisory. Moving to AGP 8.10+ lets you delete that line.
- The Gradle wrapper JAR is not committed (see *Building it*).
- `PHONE_ACCOUNT_ID` → SIM slot mapping is best-effort: the column holds the
  subscription id on most devices, the ICCID on some, and something else on
  others. `sim_slot` is nullable in the contract for exactly this reason.
- Whether an OEM recordings folder is MediaStore-indexed is emergent behaviour,
  not a contract, and it fails outright when a `.nomedia` file is present. That
  is why a persisted SAF tree is offered as a first-class access path rather than
  a fallback.
- OEM settings deep links (`com.miui.securitycenter/...` and friends) are
  best-effort component names; each one falls back to the app's own settings page
  when it does not resolve.
