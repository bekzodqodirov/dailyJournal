package uz.miya.companion.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface PhoneEventDao {

    /**
     * The same event harvested twice (a failed post keeps the high-water mark
     * behind, so the next sweep re-reads the provider) collapses silently onto
     * the existing row — the primary key is the server event_key.
     */
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertIgnore(rows: List<PhoneEventEntity>): List<Long>

    /** The next batch to POST, oldest first. The server caps a batch at 200. */
    @Query(
        "SELECT * FROM phone_events WHERE kind = :kind AND state = 'PENDING' " +
            "ORDER BY createdAt ASC, `key` ASC LIMIT :limit"
    )
    suspend fun nextBatch(kind: String, limit: Int): List<PhoneEventEntity>

    /**
     * A 200 marks EVERY event of the batch done — accepted and duplicates
     * alike, and rejected ones too: the server named its reason once, and
     * re-posting an event it rejected fails the same validation forever.
     */
    @Query("UPDATE phone_events SET state = 'DONE' WHERE `key` IN (:keys)")
    suspend fun markDone(keys: List<String>)

    @Query(
        "UPDATE phone_events SET state = :state, attempts = attempts + 1 " +
            "WHERE `key` IN (:keys)"
    )
    suspend fun markFailed(keys: List<String>, state: String)

    @Query("SELECT COUNT(*) FROM phone_events WHERE state = 'PENDING'")
    suspend fun pendingCount(): Int

    @Query("SELECT COUNT(*) FROM phone_events WHERE kind = :kind AND state = 'PENDING'")
    suspend fun pendingCountOf(kind: String): Int

    @Query("SELECT COUNT(*) FROM phone_events WHERE state = 'FAILED_PERMANENT'")
    suspend fun failedCount(): Int

    @Query(
        "SELECT COUNT(*) FROM phone_events " +
            "WHERE kind = :kind AND state = 'FAILED_PERMANENT'"
    )
    suspend fun failedCountOf(kind: String): Int

    @Query("SELECT COUNT(*) FROM phone_events WHERE state = 'PENDING'")
    fun observePendingCount(): Flow<Int>

    /** Delivered rows older than [before]; the provider is the durable copy. */
    @Query("DELETE FROM phone_events WHERE state = 'DONE' AND createdAt < :before")
    suspend fun pruneDoneBefore(before: Long)
}
