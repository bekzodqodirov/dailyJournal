package uz.miya.companion.ingest

import android.content.Context
import android.net.Uri
import android.os.Environment
import android.webkit.MimeTypeMap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import uz.miya.companion.data.Correlation
import uz.miya.companion.data.Direction
import uz.miya.companion.data.Prefs
import uz.miya.companion.data.RecordingRef
import uz.miya.companion.data.RecordingRepository
import uz.miya.companion.data.UploadEntity
import uz.miya.companion.discover.FolderProbe
import uz.miya.companion.discover.MediaStoreQuery
import uz.miya.companion.discover.SafScanner
import uz.miya.companion.util.Logx
import uz.miya.companion.util.Notifications
import uz.miya.companion.util.StorageAccess
import uz.miya.companion.util.TimeFmt
import uz.miya.companion.work.Scheduling
import java.io.File

/**
 * One scan: find candidate files, prove each one is finished, hash it, attach
 * whatever metadata the phone can honestly supply, and put it in the durable
 * queue. Nothing here talks to the network.
 *
 * This is called from three places — the call-end trigger (primary), the
 * FileObserver/ContentObserver bursts, and the 15-minute reconciliation
 * ScanWorker (a net, not the mechanism).
 */
class RecordingScanner(
    private val context: Context,
    private val prefs: Prefs,
    private val repo: RecordingRepository,
) {

    data class ScanResult(
        val inspected: Int,
        val enqueued: Int,
        val skippedUnstable: Int,
        val skippedTooShort: Int,
        val blocked: String?,
    )

    suspend fun scan(): ScanResult = withContext(Dispatchers.IO) {
        val snapshot = prefs.snapshot()

        // §4.5: every scan begins with a precondition check. A revoked
        // permission must produce a visible failure, never a silent no-op.
        val hasMedia = StorageAccess.hasMediaAudio(context)
        val hasAllFiles = StorageAccess.allFiles()
        val tree = snapshot.treeUri?.let(Uri::parse)
        val hasTree = tree != null && SafScanner.stillGranted(context, tree)

        // Files the owner shared into MIYA by hand are always processed: they
        // live in our own filesDir and need no permission at all. On a Google
        // Dialer phone this is the only path that ever produces anything.
        val sharedRefs = sharedImports()

        if (!hasMedia && !hasAllFiles && !hasTree && sharedRefs.isEmpty()) {
            val message = "MIYA cannot read any recordings folder: no media permission, " +
                "no all-files access, and no folder grant."
            prefs.setLastError(message)
            Notifications.alert(
                context,
                Notifications.ID_PRECONDITION,
                "MIYA cannot see your recordings",
                message,
            )
            return@withContext ScanResult(0, 0, 0, 0, message)
        }
        Notifications.cancel(context, Notifications.ID_PRECONDITION)

        val deviceId = prefs.deviceId()
        val region = PhoneNormalizer.regionCode(context)
        val recordedBy = FolderProbe.defaultDialerPackage(context)
        val minDuration = snapshot.minDurationSeconds

        val candidates = sharedRefs +
            collect(snapshot.folderRelativePath, tree.takeIf { hasTree }, hasAllFiles)
        Logx.i("Scan found ${candidates.size} candidate file(s)")

        var enqueued = 0
        var unstable = 0
        var tooShort = 0

        // Oldest first, and a ceiling on how many NEW files one scan touches.
        //
        // Everything below the `alreadySeen` line costs real I/O: a stability
        // check, MediaMetadataRetriever, and a full streaming SHA-256. A first
        // run on a phone with a year of call recordings can see several hundred
        // candidates, which is comfortably past WorkManager's hard 10-minute
        // execution limit and eats into the Android 15 dataSync budget when the
        // same walk runs inside IngestService. Capping means a large backlog
        // drains over several sweeps instead of failing every sweep.
        val ordered = candidates.sortedBy { it.lastModifiedMillis }
        var processed = 0
        var hitCap = false

        for (ref in ordered) {
            if (repo.alreadySeen(ref)) continue
            if (processed >= MAX_NEW_PER_SCAN) {
                hitCap = true
                break
            }
            processed++

            val verdict = StabilityGate.check(context, ref)
            if (!verdict.ready) {
                unstable++
                Logx.d("Not ready: ${Logx.redactName(ref.displayName)} (${verdict.reason})")
                continue
            }

            val containerSeconds = ((verdict.durationMillis ?: 0L) / 1000L).toInt()

            val hash = try {
                Hasher.sha256(context, ref.uri)
            } catch (t: Throwable) {
                Logx.w("Cannot hash ${Logx.redactName(ref.displayName)}: ${t.message}")
                unstable++
                continue
            }

            // Same bytes already known (a rewritten copy, a second observer, or
            // the file moved). Re-point the existing row at the new location so
            // the next sweep short-circuits instead of hashing it again.
            val existing = repo.byHash(hash.sha256)
            if (existing != null) {
                // Second precision on the mtime comparison, for the same reason
                // as in `alreadySeen`: MediaStore rounds to whole seconds and
                // SAF does not, and a rebind triggered by precision alone made
                // the two access paths fight over the row on every sweep.
                if (existing.sourceUri != ref.key ||
                    existing.mtimeMillis / 1000L != ref.lastModifiedMillis / 1000L
                ) {
                    repo.rebindSource(hash.sha256, ref)
                }
                continue
            }

            val row = buildRow(
                ref = ref,
                sha = hash.sha256,
                sizeBytes = hash.sizeBytes,
                containerSeconds = containerSeconds,
                deviceId = deviceId,
                region = region,
                recordedBy = recordedBy,
                languageHint = snapshot.languageHint,
            )

            // Drop misdials before anyone pays ElevenLabs to transcribe them.
            val effectiveSeconds = row.durationSeconds ?: containerSeconds
            if (effectiveSeconds in 0 until minDuration) {
                tooShort++
                Logx.d(
                    "Skipping ${Logx.redactName(ref.displayName)}: " +
                        "${effectiveSeconds}s < ${minDuration}s"
                )
                repo.enqueueSkipped(row, "shorter than the ${minDuration}s floor")
                continue
            }

            if (repo.enqueue(row)) enqueued++
        }

        prefs.markScanned()
        runCatching { repo.purgeOldDone() }

        // Best effort continuation for a backlog. It is deliberately only best
        // effort: the burst work is unique-KEEP, so this is dropped when a scan
        // is already queued, and the 15-minute reconciliation sweep is the
        // guarantee. Only asked for when the scan actually made progress, so a
        // folder full of files that can never pass the gates cannot become a
        // rescan loop.
        if (hitCap && enqueued + tooShort > 0) {
            Logx.i("Scan cap reached ($MAX_NEW_PER_SCAN new files); continuing on the next sweep")
            Scheduling.enqueueBurstScan(context)
        }

        ScanResult(candidates.size, enqueued, unstable, tooShort, null)
    }

    /** Recordings imported through "Share to MIYA". Ours to read and to delete. */
    private fun sharedImports(): List<RecordingRef> {
        val dir = File(context.filesDir, SHARED_DIR)
        if (!dir.isDirectory) return emptyList()
        return dir.listFiles().orEmpty()
            .filter { it.isFile && FolderProbe.isAudioName(it.name) }
            .map { f ->
                RecordingRef(
                    uri = Uri.fromFile(f),
                    // Strip the UUID prefix we added so the server sees the
                    // filename the dialer actually used.
                    displayName = f.name.substringAfter(SHARED_SEPARATOR, f.name)
                        .ifBlank { f.name },
                    sizeBytes = f.length(),
                    lastModifiedMillis = f.lastModified(),
                    mimeType = mimeFor(f.name),
                    absolutePath = f.absolutePath,
                    ownedByUs = true,
                )
            }
    }

    /**
     * Candidates from every access path we hold, de-duplicated by
     * (name, size, mtime) so a file visible through both MediaStore and a SAF
     * tree is not hashed twice on every sweep.
     */
    private fun collect(
        configuredFolder: String?,
        treeUri: Uri?,
        hasAllFiles: Boolean,
    ): List<RecordingRef> {
        // NOTHING is uploaded out of an unconfirmed folder.
        //
        // OemCandidates.ordered() exists to be PROBED, and it contains very
        // broad roots — Recordings/, Sounds/, Call/, Calls/, Truecaller/ — that
        // MediaStore matches with RELATIVE_PATH LIKE 'Recordings/%', i.e. every
        // descendant. Scanning those before the owner has confirmed anything
        // meant voice memos, ringtones and every other indexed audio file under
        // those trees were hashed, queued and sent to the server as if they
        // were call recordings: the owner's private non-call audio leaving the
        // phone without their ever having pointed at it. FolderProbe still
        // probes the whole candidate list (it samples five rows per folder and
        // enqueues nothing), and confirming a folder — Auto-detect or Pick by
        // hand — is what opens this path.
        if (configuredFolder == null && treeUri == null) {
            Logx.i("No confirmed recordings folder yet; scanning shared imports only")
            return emptyList()
        }

        val seen = LinkedHashMap<String, RecordingRef>()

        fun add(ref: RecordingRef) {
            if (!FolderProbe.isAudioName(ref.displayName)) return
            // Second precision on the mtime: MediaStore reports DATE_MODIFIED
            // in whole seconds and DocumentFile.lastModified() in true millis,
            // so a millisecond-exact key listed the same physical file twice —
            // one entry matching the queue's source index and one not, which
            // made every sweep re-hash and re-bind every file, for ever.
            val key = "${ref.displayName}|${ref.sizeBytes}|${ref.lastModifiedMillis / 1000L}"
            seen.putIfAbsent(key, ref)
        }

        // 1. The SAF tree the owner picked. Immune to .nomedia, so it goes
        //    first — and, because of putIfAbsent, it is the ref that wins when
        //    a file is visible through both paths.
        if (treeUri != null) {
            SafScanner.list(context, treeUri).forEach(::add)
        }

        // 2. MediaStore, only under the confirmed folder.
        val folders = listOfNotNull(configuredFolder)
        for (folder in folders) {
            MediaStoreQuery.listUnder(context, folder, limit = 400).forEach(::add)
        }

        // 3. Direct filesystem, only in opt-in all-files mode.
        if (hasAllFiles) {
            val root = Environment.getExternalStorageDirectory()
            for (folder in folders) {
                val dir = File(root, folder)
                dir.listFiles()?.forEach { f ->
                    if (f.isFile) {
                        add(
                            RecordingRef(
                                uri = Uri.fromFile(f),
                                displayName = f.name,
                                sizeBytes = f.length(),
                                lastModifiedMillis = f.lastModified(),
                                mimeType = mimeFor(f.name),
                                absolutePath = f.absolutePath,
                            )
                        )
                    }
                }
            }
        }

        return seen.values.toList()
    }

    private fun buildRow(
        ref: RecordingRef,
        sha: String,
        sizeBytes: Long,
        containerSeconds: Int,
        deviceId: String,
        region: String,
        recordedBy: String?,
        languageHint: String?,
    ): UploadEntity {
        val parsed = FilenameParser.parse(ref.displayName)
        val match = CallLogMatcher.match(context, ref.lastModifiedMillis, parsed.rawNumber)

        val correlation: String
        val startedAtMillis: Long
        val durationSeconds: Int?
        val direction: String
        val callId: String?
        var counterparty: String?
        var rawNumber: String?
        val simSlot: Int?

        if (match != null) {
            correlation = Correlation.CALL_LOG
            startedAtMillis = match.startedAtMillis
            durationSeconds = match.durationSeconds
            direction = match.direction
            callId = "$deviceId:${match.callLogId}"
            counterparty = match.cachedName
            rawNumber = match.number
            simSlot = match.simSlot
        } else if (parsed.startedAtMillis != null) {
            correlation = Correlation.FILENAME
            startedAtMillis = parsed.startedAtMillis
            durationSeconds = containerSeconds.takeIf { it > 0 }
            direction = Direction.UNKNOWN
            callId = null
            counterparty = parsed.nameHint
            rawNumber = parsed.rawNumber
            simSlot = null
        } else {
            // mtime is call END on every OEM we know of, so subtract the
            // container duration to get something closer to the truth than
            // "the moment the file finished being written".
            correlation = Correlation.MTIME
            startedAtMillis = ref.lastModifiedMillis - containerSeconds * 1000L
            durationSeconds = containerSeconds.takeIf { it > 0 }
            direction = Direction.UNKNOWN
            callId = null
            counterparty = parsed.nameHint
            rawNumber = parsed.rawNumber
            simSlot = null
        }

        if (counterparty.isNullOrBlank() && rawNumber != null) {
            counterparty = CallLogMatcher.contactName(context, rawNumber)
        }
        if (rawNumber.isNullOrBlank()) rawNumber = parsed.rawNumber

        return UploadEntity(
            sha256 = sha,
            sourceUri = ref.key,
            displayName = ref.displayName,
            sizeBytes = sizeBytes,
            mtimeMillis = ref.lastModifiedMillis,
            ownedByUs = ref.ownedByUs,
            callId = callId,
            startedAtMillis = startedAtMillis,
            startedAtIso = TimeFmt.isoOffset(startedAtMillis),
            durationSeconds = durationSeconds,
            direction = direction,
            counterpartyName = counterparty?.trim()?.takeIf { it.isNotEmpty() },
            phoneE164 = PhoneNormalizer.toE164(rawNumber, region),
            locale = languageHint,
            simSlot = simSlot,
            mime = ref.mimeType ?: mimeFor(ref.displayName),
            recordedBy = recordedBy,
            correlation = correlation,
        )
    }

    private fun mimeFor(name: String): String {
        val ext = name.substringAfterLast('.', "").lowercase()
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext)
            ?: when (ext) {
                "m4a", "aac" -> "audio/mp4"
                "mp3" -> "audio/mpeg"
                "amr", "awb" -> "audio/amr"
                "wav" -> "audio/wav"
                "ogg", "opus" -> "audio/ogg"
                else -> "audio/mp4"
            }
    }

    private companion object {
        const val SHARED_DIR = "shared"
        const val SHARED_SEPARATOR = "__"

        /**
         * New files hashed per scan. 50 x (stability check + container parse +
         * streaming SHA-256) fits inside WorkManager's 10-minute ceiling with
         * room to spare on a slow eMMC; a bigger backlog drains across sweeps.
         */
        const val MAX_NEW_PER_SCAN = 50
    }
}
