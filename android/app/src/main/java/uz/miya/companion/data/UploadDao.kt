package uz.miya.companion.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface UploadDao {

    /** Dedupe layer 1: identical bytes collapse onto one row, silently. */
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertIgnore(row: UploadEntity): Long

    @Query("SELECT * FROM uploads WHERE sha256 = :sha")
    suspend fun byHash(sha: String): UploadEntity?

    /**
     * Dedupe layer 2, at SECOND precision.
     *
     * The same physical file reports different mtime precision depending on
     * how it was found: MediaStore's DATE_MODIFIED is whole seconds (scaled up
     * to millis), while DocumentFile.lastModified() is true milliseconds. An
     * exact comparison therefore missed on every file whose mtime has a
     * non-zero millisecond part, and the file was re-read and re-hashed on
     * every sweep, for ever.
     */
    @Query(
        "SELECT COUNT(*) FROM uploads " +
            "WHERE sourceUri = :uri AND sizeBytes = :size AND mtimeMillis / 1000 = :mtimeSeconds"
    )
    suspend fun countBySource(uri: String, size: Long, mtimeSeconds: Long): Int

    @Query(
        "SELECT * FROM uploads WHERE state IN ('PENDING','UPLOADING') " +
            "ORDER BY startedAtMillis ASC LIMIT :limit"
    )
    suspend fun nextPending(limit: Int): List<UploadEntity>

    /**
     * What DrainWorker re-arms. FAILED_PRECONDITION is included on purpose:
     * those rows are blocked by something on the PHONE (no server URL yet, no
     * token yet, a permission that was revoked and restored), and nothing else
     * ever re-queues them — so on a first install every recording found before
     * step 10 of onboarding stayed blocked until the owner found "Retry all".
     *
     * FAILED_PERMANENT is deliberately NOT included: the server said never
     * send this again, and only an explicit "Retry all" overrides that.
     */
    @Query(
        "SELECT * FROM uploads WHERE state IN ('PENDING','UPLOADING','FAILED_PRECONDITION') " +
            "ORDER BY startedAtMillis ASC LIMIT :limit"
    )
    suspend fun nextRetryable(limit: Int): List<UploadEntity>

    @Query("SELECT * FROM uploads ORDER BY startedAtMillis DESC LIMIT :limit")
    fun observeRecent(limit: Int): Flow<List<UploadEntity>>

    @Query("SELECT COUNT(*) FROM uploads WHERE state IN ('PENDING','UPLOADING')")
    fun observeQueueDepth(): Flow<Int>

    @Query("SELECT COUNT(*) FROM uploads WHERE state IN ('FAILED_PERMANENT','FAILED_PRECONDITION')")
    fun observeFailedCount(): Flow<Int>

    @Query("SELECT MAX(uploadedAt) FROM uploads WHERE state = 'DONE'")
    fun observeLastUploadAt(): Flow<Long?>

    @Query("SELECT MAX(uploadedAt) FROM uploads WHERE state = 'DONE'")
    suspend fun lastUploadAt(): Long?

    @Query(
        "UPDATE uploads SET state = :state, lastError = :error, attempts = :attempts, " +
            "nextAttemptAt = :nextAttemptAt, updatedAt = :now WHERE sha256 = :sha"
    )
    suspend fun updateState(
        sha: String,
        state: String,
        error: String?,
        attempts: Int,
        nextAttemptAt: Long,
        now: Long,
    )

    @Query(
        "UPDATE uploads SET state = 'DONE', lastError = NULL, uploadedAt = :now, " +
            "updatedAt = :now WHERE sha256 = :sha"
    )
    suspend fun markDone(sha: String, now: Long)

    @Query("UPDATE uploads SET state = 'PENDING', nextAttemptAt = 0, lastError = NULL, updatedAt = :now WHERE sha256 = :sha")
    suspend fun requeue(sha: String, now: Long)

    @Query("UPDATE uploads SET state = 'PENDING', nextAttemptAt = 0, lastError = NULL, updatedAt = :now WHERE state IN ('FAILED_PERMANENT','FAILED_PRECONDITION','UPLOADING')")
    suspend fun requeueAllFailed(now: Long)

    /**
     * Same bytes, new location (the file was renamed or moved). Re-point the
     * row so the next sweep short-circuits on the source index instead of
     * hashing the file again.
     */
    @Query(
        "UPDATE uploads SET sourceUri = :uri, sizeBytes = :size, mtimeMillis = :mtime, " +
            "displayName = :name, updatedAt = :now WHERE sha256 = :sha"
    )
    suspend fun rebindSource(
        sha: String,
        uri: String,
        size: Long,
        mtime: Long,
        name: String,
        now: Long,
    )

    @Query("DELETE FROM uploads WHERE sha256 = :sha")
    suspend fun delete(sha: String)

    @Query("SELECT * FROM uploads WHERE state = 'DONE' AND uploadedAt IS NOT NULL AND uploadedAt < :before")
    suspend fun doneBefore(before: Long): List<UploadEntity>

    @Query("SELECT sha256 FROM uploads")
    suspend fun allHashes(): List<String>
}
