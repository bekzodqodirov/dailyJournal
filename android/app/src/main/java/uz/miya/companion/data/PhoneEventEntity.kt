package uz.miya.companion.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

object PhoneEventKind {
    const val CALL = "call"
    const val SMS = "sms"
}

/**
 * One queued phone event (build step 6): a call-log entry or an SMS, as the
 * exact JSON object that will be POSTed to /v1/phone/calls or /v1/phone/sms.
 *
 * The primary key IS the server's dedupe key (`media->>'event_key'` behind the
 * partial unique index `ux_interactions_event_key`), minted here exactly as
 * the server mints it:
 *
 *   call: "<device_id>:call:<CallLog._ID>"
 *   sms:  "<device_id>:sms:<Sms._ID>:<sha256(sender|received_at_iso|body)[:16]>"
 *
 * so a retried batch, a re-read of the provider after a failed post, and the
 * server's own uniqueness check all collapse onto one row. The SMS key hashes
 * the content because Android's `Sms._ID` restarts after a wipe.
 *
 * State reuses [UploadState] semantics: PENDING until the server answered 200
 * for the batch, DONE after (accepted and duplicates alike — the server's
 * response is honest, and re-sending a duplicate costs one index scan there),
 * FAILED_PERMANENT when the server said never send this again.
 */
@Entity(
    tableName = "phone_events",
    indices = [
        Index(value = ["state", "kind", "createdAt"]),
    ],
)
data class PhoneEventEntity(
    /** The server event_key; see the class comment. */
    @PrimaryKey val key: String,
    /** [PhoneEventKind.CALL] or [PhoneEventKind.SMS]. */
    val kind: String,
    /** The exact JSON object POSTed, frozen at enqueue time. */
    val payloadJson: String,
    val state: String = UploadState.PENDING,
    val attempts: Int = 0,
    val createdAt: Long = System.currentTimeMillis(),
)
