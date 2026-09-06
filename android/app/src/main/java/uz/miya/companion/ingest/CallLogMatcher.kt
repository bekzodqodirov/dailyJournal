package uz.miya.companion.ingest

import android.Manifest
import android.content.Context
import android.net.Uri
import android.provider.CallLog
import android.provider.ContactsContract
import android.telephony.SubscriptionManager
import uz.miya.companion.data.Direction
import uz.miya.companion.util.Logx
import uz.miya.companion.util.StorageAccess

/**
 * Correlates a recording file with the CallLog row that produced it.
 *
 * READ_CALL_LOG is HARD-RESTRICTED on Android 10+: it is allowlisted by the
 * installer at install time and a user cannot grant it by hand, so a plain
 * `adb install` or file-manager sideload may leave this permanently ungrantable.
 * Every method here therefore returns null rather than throwing, and the caller
 * degrades to FilenameParser + mtime. That degraded path is not an edge case —
 * on a sideloaded APK it may be the only path that ever runs.
 */
object CallLogMatcher {

    data class Match(
        val callLogId: Long,
        val startedAtMillis: Long,
        val durationSeconds: Int,
        val direction: String,
        val number: String?,
        val cachedName: String?,
        val simSlot: Int?,
    )

    /** How far outside the [start, start+duration] window we still accept. */
    private const val TOLERANCE_MILLIS = 5 * 60 * 1000L

    /** How far back to look for candidate rows. */
    private const val LOOKBACK_MILLIS = 12 * 60 * 60 * 1000L

    fun available(context: Context): Boolean =
        StorageAccess.granted(context, Manifest.permission.READ_CALL_LOG)

    /**
     * The file's mtime is the best proxy we have for call END, so the row we
     * want is the one whose [DATE, DATE + DURATION] window contains it. A file
     * written a few seconds after hang-up is normal, so the window is widened
     * by TOLERANCE_MILLIS on both sides and the closest end-time wins.
     */
    fun match(context: Context, fileMtimeMillis: Long, filenameNumber: String?): Match? {
        if (!available(context)) return null

        val projection = arrayOf(
            CallLog.Calls._ID,
            CallLog.Calls.NUMBER,
            CallLog.Calls.DATE,
            CallLog.Calls.DURATION,
            CallLog.Calls.TYPE,
            CallLog.Calls.CACHED_NAME,
            CallLog.Calls.PHONE_ACCOUNT_ID,
        )
        val selection = "${CallLog.Calls.DATE} >= ? AND ${CallLog.Calls.DATE} <= ?"
        val args = arrayOf(
            (fileMtimeMillis - LOOKBACK_MILLIS).toString(),
            (fileMtimeMillis + TOLERANCE_MILLIS).toString(),
        )

        var best: Match? = null
        var bestScore = Long.MAX_VALUE

        try {
            context.contentResolver.query(
                CallLog.Calls.CONTENT_URI,
                projection,
                selection,
                args,
                "${CallLog.Calls.DATE} DESC",
            )?.use { c ->
                val idIdx = c.getColumnIndexOrThrow(CallLog.Calls._ID)
                val numIdx = c.getColumnIndexOrThrow(CallLog.Calls.NUMBER)
                val dateIdx = c.getColumnIndexOrThrow(CallLog.Calls.DATE)
                val durIdx = c.getColumnIndexOrThrow(CallLog.Calls.DURATION)
                val typeIdx = c.getColumnIndexOrThrow(CallLog.Calls.TYPE)
                val nameIdx = c.getColumnIndexOrThrow(CallLog.Calls.CACHED_NAME)
                val acctIdx = c.getColumnIndex(CallLog.Calls.PHONE_ACCOUNT_ID)

                var inspected = 0
                while (c.moveToNext() && inspected < 200) {
                    inspected++
                    val start = c.getLong(dateIdx)
                    val durationSeconds = c.getLong(durIdx)
                    val end = start + durationSeconds * 1000L
                    val number = c.getString(numIdx)

                    // Distance from the call's end to the file's mtime.
                    val distance = when {
                        fileMtimeMillis in start..end -> 0L
                        fileMtimeMillis > end -> fileMtimeMillis - end
                        else -> start - fileMtimeMillis
                    }
                    if (distance > TOLERANCE_MILLIS) continue

                    // A filename number, when we have one, breaks ties between
                    // two calls that ended within seconds of each other.
                    val numberBonus =
                        if (filenameNumber != null &&
                            PhoneNormalizer.looselyEqual(filenameNumber, number)
                        ) 0L else 1_000L

                    val score = distance + numberBonus
                    if (score < bestScore) {
                        bestScore = score
                        best = Match(
                            callLogId = c.getLong(idIdx),
                            startedAtMillis = start,
                            durationSeconds = durationSeconds.toInt(),
                            direction = directionOf(c.getInt(typeIdx)),
                            number = number,
                            cachedName = c.getString(nameIdx)?.takeIf { it.isNotBlank() },
                            simSlot = if (acctIdx >= 0) {
                                slotFor(context, c.getString(acctIdx))
                            } else {
                                null
                            },
                        )
                    }
                }
            }
        } catch (t: Throwable) {
            Logx.w("CallLog query failed (permission likely ungrantable): ${t.message}")
            return null
        }
        return best
    }

    private fun directionOf(type: Int): String = when (type) {
        CallLog.Calls.INCOMING_TYPE -> Direction.INCOMING
        CallLog.Calls.OUTGOING_TYPE -> Direction.OUTGOING
        CallLog.Calls.MISSED_TYPE -> Direction.MISSED
        CallLog.Calls.REJECTED_TYPE -> Direction.REJECTED
        else -> Direction.UNKNOWN
    }

    /**
     * PHONE_ACCOUNT_ID is usually the subscription id, sometimes the ICCID,
     * sometimes something else entirely. Best effort; null is fine.
     */
    private fun slotFor(context: Context, phoneAccountId: String?): Int? {
        if (phoneAccountId.isNullOrBlank()) return null
        return try {
            val sm = context.getSystemService(SubscriptionManager::class.java) ?: return null
            @Suppress("MissingPermission")
            val list = sm.activeSubscriptionInfoList ?: return null
            val asInt = phoneAccountId.toIntOrNull()
            list.firstOrNull { info ->
                (asInt != null && info.subscriptionId == asInt) ||
                    info.iccId == phoneAccountId
            }?.simSlotIndex
        } catch (t: Throwable) {
            null
        }
    }

    /**
     * The call log caches a display name only if the contact existed when the
     * call happened. When it does not, look the number up directly — a name is
     * what lets the server attach a debt to the right Person, so it is worth
     * one extra query.
     */
    fun contactName(context: Context, number: String?): String? {
        if (number.isNullOrBlank()) return null
        if (!StorageAccess.granted(context, Manifest.permission.READ_CONTACTS)) return null
        return try {
            val uri = Uri.withAppendedPath(
                ContactsContract.PhoneLookup.CONTENT_FILTER_URI,
                Uri.encode(number),
            )
            context.contentResolver.query(
                uri,
                arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME),
                null,
                null,
                null,
            )?.use { c ->
                if (c.moveToFirst()) c.getString(0)?.takeIf { it.isNotBlank() } else null
            }
        } catch (t: Throwable) {
            Logx.w("Contact lookup failed: ${t.message}")
            null
        }
    }
}
