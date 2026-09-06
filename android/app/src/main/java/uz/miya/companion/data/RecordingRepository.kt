package uz.miya.companion.data

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager
import uz.miya.companion.util.Logx
import uz.miya.companion.work.UploadWorker
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * The only writer of queue state.
 *
 * The queue is Room-backed and every row is written BEFORE any network call is
 * attempted, which is what makes airplane mode, a reboot, a force-stop and an
 * app update all cost latency instead of data.
 */
class RecordingRepository(
    private val context: Context,
    private val dao: UploadDao,
    private val prefs: Prefs,
) {

    val recent = dao.observeRecent(200)
    val queueDepth = dao.observeQueueDepth()
    val failedCount = dao.observeFailedCount()
    val lastUploadAt = dao.observeLastUploadAt()

    /**
     * True if this (uri, size, mtime-to-the-second) has already been hashed.
     *
     * Second precision, not millisecond: MediaStore reports DATE_MODIFIED in
     * whole seconds and SAF reports true milliseconds for the same file, and
     * an exact match made the two disagree for ever.
     */
    suspend fun alreadySeen(ref: RecordingRef): Boolean =
        dao.countBySource(ref.key, ref.sizeBytes, ref.lastModifiedMillis / 1000L) > 0

    suspend fun byHash(sha: String): UploadEntity? = dao.byHash(sha)

    /**
     * Insert if new and schedule an upload. Returns true if this call created
     * the row — a duplicate CLOSE_WRITE, a MediaStore rescan after reboot and a
     * ScanWorker racing IngestService all converge here and only one wins.
     */
    suspend fun enqueue(row: UploadEntity): Boolean {
        val inserted = dao.insertIgnore(row) != -1L
        if (!inserted) {
            Logx.d("Already queued: ${row.sha256.take(12)}")
            return false
        }
        scheduleUpload(row.sha256)
        return true
    }

    suspend fun scheduleUpload(sha: String) {
        val snapshot = prefs.snapshot()
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(
                if (snapshot.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED
            )
            .build()

        val request = OneTimeWorkRequestBuilder<UploadWorker>()
            .setInputData(Data.Builder().putString(UploadWorker.KEY_SHA, sha).build())
            .setConstraints(constraints)
            // 30 s -> 1 m -> 2 m -> ... WorkManager caps the interval at 5 h.
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .addTag(TAG_UPLOAD)
            .build()

        // KEEP, not REPLACE: if an attempt for this hash is already in flight
        // or mid-backoff, a second scan must not restart its backoff clock.
        WorkManager.getInstance(context)
            .enqueueUniqueWork("upload-$sha", ExistingWorkPolicy.KEEP, request)
    }

    /** Record a row we deliberately will not upload, so it is never re-hashed. */
    suspend fun enqueueSkipped(row: UploadEntity, reason: String): Boolean =
        dao.insertIgnore(row.copy(state = UploadState.SKIPPED, lastError = reason)) != -1L

    suspend fun rebindSource(sha: String, ref: RecordingRef) {
        dao.rebindSource(
            sha, ref.key, ref.sizeBytes, ref.lastModifiedMillis, ref.displayName,
            System.currentTimeMillis(),
        )
    }

    suspend fun markUploading(sha: String, attempts: Int) {
        dao.updateState(sha, UploadState.UPLOADING, null, attempts, 0L, System.currentTimeMillis())
    }

    suspend fun markDone(sha: String) {
        dao.markDone(sha, System.currentTimeMillis())
        prefs.setLastError(null)
        prefs.setAuthFailed(false)
    }

    /**
     * `nextAttemptAt` is the earliest moment this row is worth touching again.
     * WorkManager owns the real schedule; this mirrors it (30 s base, doubling,
     * capped at 5 h, exactly as configured in [scheduleUpload]) so that
     * DrainWorker can tell "waiting for its own backoff" from "orphaned and
     * needs re-arming". It used to be written as `now`, i.e. always in the
     * past, and read by nothing at all.
     */
    suspend fun markRetry(sha: String, attempts: Int, error: String) {
        val now = System.currentTimeMillis()
        dao.updateState(
            sha, UploadState.PENDING, error, attempts,
            now + backoffMillis(attempts), now,
        )
        prefs.setLastError(error)
    }

    private fun backoffMillis(attempts: Int): Long {
        // Bounded shift: 30 s << 20 is still far inside Long, and the result is
        // clamped to the 5 h WorkManager itself caps at.
        val steps = (attempts - 1).coerceIn(0, 20)
        return (BACKOFF_BASE_MILLIS shl steps).coerceAtMost(BACKOFF_MAX_MILLIS)
    }

    suspend fun markPermanent(sha: String, attempts: Int, error: String) {
        dao.updateState(
            sha, UploadState.FAILED_PERMANENT, error, attempts, 0L, System.currentTimeMillis(),
        )
        prefs.setLastError(error)
    }

    suspend fun markPrecondition(sha: String, error: String) {
        val row = dao.byHash(sha) ?: return
        dao.updateState(
            sha, UploadState.FAILED_PRECONDITION, error, row.attempts, 0L,
            System.currentTimeMillis(),
        )
        prefs.setLastError(error)
    }

    /**
     * §2.7: a sha256 mismatch means the bytes we hashed and the bytes that
     * arrived differ. Re-hash and try once more — but the hash is the primary
     * key, so "update the hash" is delete-and-reinsert.
     */
    suspend fun rehash(old: UploadEntity, newSha: String, newSize: Long): UploadEntity? {
        if (newSha == old.sha256) return null
        dao.delete(old.sha256)
        val replacement = old.copy(
            sha256 = newSha,
            sizeBytes = newSize,
            state = UploadState.PENDING,
            attempts = old.attempts + 1,
            lastError = "re-hashed after server sha256 mismatch",
            hashRetried = true,
            updatedAt = System.currentTimeMillis(),
        )
        dao.insertIgnore(replacement)
        scheduleUpload(newSha)
        return replacement
    }

    suspend fun retryNow(sha: String) {
        dao.requeue(sha, System.currentTimeMillis())
        scheduleUpload(sha)
    }

    suspend fun retryAllFailed() {
        dao.requeueAllFailed(System.currentTimeMillis())
        prefs.setAuthFailed(false)
        for (row in dao.nextPending(500)) scheduleUpload(row.sha256)
    }

    /**
     * What DrainWorker re-arms after a reboot, a force-stop or an app start:
     * everything still in flight PLUS the rows blocked on a phone-side
     * precondition (no server URL yet, no token yet), minus anything that is
     * simply waiting out its own backoff.
     */
    suspend fun retryableRows(limit: Int = 500): List<UploadEntity> {
        val now = System.currentTimeMillis()
        return dao.nextRetryable(limit).filter { it.nextAttemptAt <= now }
    }

    /**
     * Optional, default OFF. Only ever deletes a file when the owner explicitly
     * asked for it, and never one we merely read — deleting the OEM dialer's
     * own recording out from under the Phone app's UI is not our business
     * unless told.
     */
    suspend fun deleteSourceIfRequested(row: UploadEntity) {
        val snapshot = prefs.snapshot()
        if (!row.ownedByUs && !snapshot.deleteAfterUpload) return
        val uri = Uri.parse(row.sourceUri)
        try {
            when (uri.scheme) {
                "file" -> uri.path?.let { File(it).delete() }
                "content" -> {
                    if (DocumentsContract.isDocumentUri(context, uri)) {
                        DocumentsContract.deleteDocument(context.contentResolver, uri)
                    } else {
                        context.contentResolver.delete(uri, null, null)
                    }
                }
            }
        } catch (t: Throwable) {
            Logx.w("Could not delete source after upload: ${t.message}")
        }
    }

    /**
     * Keep DONE rows for 90 days, but only drop the ones whose source file is
     * genuinely gone. Dropping a row while the file is still on disk means the
     * next sweep re-hashes and re-probes it forever.
     */
    suspend fun purgeOldDone() {
        val cutoff = System.currentTimeMillis() - 90L * 24 * 60 * 60 * 1000
        for (row in dao.doneBefore(cutoff)) {
            val gone = try {
                context.contentResolver.openInputStream(Uri.parse(row.sourceUri))
                    ?.use { false } ?: true
            } catch (t: Throwable) {
                true
            }
            if (gone) dao.delete(row.sha256)
        }
    }

    fun refFor(row: UploadEntity): RecordingRef = RecordingRef(
        uri = Uri.parse(row.sourceUri),
        displayName = row.displayName,
        sizeBytes = row.sizeBytes,
        lastModifiedMillis = row.mtimeMillis,
        mimeType = row.mime,
        absolutePath = null,
        ownedByUs = row.ownedByUs,
    )

    companion object {
        const val TAG_UPLOAD = "miya-upload"

        /** Mirrors the setBackoffCriteria in [scheduleUpload]. */
        private const val BACKOFF_BASE_MILLIS = 30_000L
        private const val BACKOFF_MAX_MILLIS = 5L * 60 * 60 * 1000
    }
}
