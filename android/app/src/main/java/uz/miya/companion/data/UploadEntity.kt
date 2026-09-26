package uz.miya.companion.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

object UploadState {
    /** Queued, waiting for a network window. */
    const val PENDING = "PENDING"
    /** A worker currently has it. */
    const val UPLOADING = "UPLOADING"
    /** Server said 202 accepted or 200 duplicate. Both are success. */
    const val DONE = "DONE"
    /** Server said "never send this again" (oversize, bad shape). */
    const val FAILED_PERMANENT = "FAILED_PERMANENT"
    /** Something on the phone is wrong: permission revoked, file vanished. */
    const val FAILED_PRECONDITION = "FAILED_PRECONDITION"

    /**
     * Deliberately not uploaded — a 0-3 second misdial below the configured
     * floor. The row still exists so the file is never hashed again; hashing
     * every file on every sweep is the unbounded-I/O mistake this design calls
     * out on the server side and must not repeat on the phone.
     */
    const val SKIPPED = "SKIPPED"
}

object Direction {
    const val INCOMING = "incoming"
    const val OUTGOING = "outgoing"
    const val MISSED = "missed"
    const val REJECTED = "rejected"
    const val UNKNOWN = "unknown"
}

object Correlation {
    const val CALL_LOG = "call_log"
    const val FILENAME = "filename"
    const val MTIME = "mtime"
}

/**
 * One row per recording. The primary key is the content hash, which is the
 * first of the four dedupe layers in §4.3: the same bytes can only ever occupy
 * one queue row, no matter how many observers fire.
 *
 * (sourceUri, sizeBytes, mtimeMillis) is indexed so a rescan can skip a file
 * it has already hashed without reading it again — hashing every file on every
 * sweep is exactly the unbounded-I/O mistake called out for the server side.
 */
@Entity(
    tableName = "uploads",
    indices = [
        Index(value = ["sourceUri", "sizeBytes", "mtimeMillis"]),
        Index(value = ["state", "nextAttemptAt"]),
        Index(value = ["callId"]),
    ],
)
data class UploadEntity(
    @PrimaryKey val sha256: String,

    val sourceUri: String,
    val displayName: String,
    val sizeBytes: Long,
    val mtimeMillis: Long,
    /** True when the bytes live in our own filesDir and we may delete them. */
    val ownedByUs: Boolean,

    // ---- the §2.5 metadata object -------------------------------------
    val callId: String?,
    val startedAtMillis: Long,
    /** ISO-8601 *with* offset, frozen at enqueue time. */
    val startedAtIso: String,
    val durationSeconds: Int?,
    val direction: String,
    val counterpartyName: String?,
    val phoneE164: String?,
    val locale: String?,
    val simSlot: Int?,
    val mime: String?,
    val recordedBy: String?,
    val correlation: String,

    // ---- queue state ---------------------------------------------------
    val state: String = UploadState.PENDING,
    val attempts: Int = 0,
    val nextAttemptAt: Long = 0L,
    val lastError: String? = null,
    /**
     * §2.7: a sha256 mismatch is retried exactly once (re-hash, re-upload)
     * before the row is parked. This flag is what "once" means.
     */
    val hashRetried: Boolean = false,

    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis(),
    val uploadedAt: Long? = null,
)
