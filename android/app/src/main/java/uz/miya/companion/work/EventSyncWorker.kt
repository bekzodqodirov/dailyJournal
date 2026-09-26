package uz.miya.companion.work

import android.content.Context
import android.provider.Telephony
import android.telephony.SubscriptionManager
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import org.json.JSONObject
import uz.miya.companion.Graph
import uz.miya.companion.data.PhoneEventDao
import uz.miya.companion.data.PhoneEventEntity
import uz.miya.companion.data.PhoneEventKind
import uz.miya.companion.data.SmsMode
import uz.miya.companion.data.UploadState
import uz.miya.companion.ingest.CallLogMatcher
import uz.miya.companion.ingest.PaymentSenders
import uz.miya.companion.ingest.PhoneNormalizer
import uz.miya.companion.net.EventPostOutcome
import uz.miya.companion.util.Logx
import uz.miya.companion.util.StorageAccess
import uz.miya.companion.util.TimeFmt
import java.security.MessageDigest
import kotlin.coroutines.cancellation.CancellationException

/**
 * Phone events (build step 6): harvest new call-log rows and SMS from the
 * providers, freeze each as one JSON payload in the Room queue, POST them in
 * batches of at most [MAX_BATCH], and advance the per-provider high-water
 * marks ONLY after the server answered 200 for everything below them.
 *
 * The providers themselves are the durable source, the Room queue is the
 * dedupe (its PK is the server event_key), and the marks are just cursors —
 * so a failed post costs a re-read of at most one batch, never data. A row
 * the server already holds comes back as a duplicate inside a 200, which is
 * success, exactly like the recordings path.
 *
 * SMS bodies are money data and are NEVER logged — not on harvest, not on
 * failure (Logx has no business seeing them; see Logx.redactSmsBody).
 */
class EventSyncWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    // Set while harvesting when a provider returned a full MAX_BATCH —
    // more rows are certainly waiting behind the high-water mark.
    private var hitHarvestCap = false

    override suspend fun doWork(): Result {
        Graph.init(applicationContext)
        return try {
            val prefs = Graph.prefs
            val snapshot = prefs.snapshot()
            val dao = Graph.database.phoneEvents()
            val deviceId = prefs.deviceId()

            // Harvest first, post second: even with the server unreachable the
            // queue fills, and the freshly minted rows ride the first post
            // that succeeds.
            hitHarvestCap = false
            val maxCallId = harvestCalls(dao, deviceId, snapshot.lastCallLogId, snapshot.uploadCallLog)
            val maxSmsId = harvestSms(dao, deviceId, snapshot.lastSmsId, snapshot.smsMode)

            if (!snapshot.serverConfigured || !Graph.tokenStore.hasToken()) {
                Logx.d("Event sync: server not configured; ${dao.pendingCount()} event(s) waiting")
                return Result.success()
            }
            if (snapshot.authFailed) {
                // The 401 latch, same as UploadWorker: retrying a bad token
                // forever is useless. Saving a new token re-runs the sync.
                Logx.w("Event sync: auth is known-bad; not posting")
                return Result.success()
            }

            val callsOk = postPending(dao, PhoneEventKind.CALL, snapshot.serverUrl, deviceId)
            val smsOk = postPending(dao, PhoneEventKind.SMS, snapshot.serverUrl, deviceId)

            // The mark moves only when every harvested row below it is
            // settled — PENDING still waiting, or FAILED_PERMANENT parked by
            // an unexpected 4xx, both hold it. When it stays behind, the next
            // sweep re-reads the same provider rows and the queue's primary
            // key absorbs them; nothing is skipped with only a Health note.
            if (callsOk && dao.pendingCountOf(PhoneEventKind.CALL) == 0 &&
                dao.failedCountOf(PhoneEventKind.CALL) == 0
            ) {
                prefs.setLastCallLogId(maxCallId)
            }
            if (smsOk && dao.pendingCountOf(PhoneEventKind.SMS) == 0 &&
                dao.failedCountOf(PhoneEventKind.SMS) == 0
            ) {
                prefs.setLastSmsId(maxSmsId)
            }

            dao.pruneDoneBefore(System.currentTimeMillis() - KEEP_DONE_MILLIS)

            // A first install or a long offline stretch leaves thousands of
            // provider rows behind the mark; one run reads at most MAX_BATCH
            // per provider, so a full harvest means more is waiting — chain
            // another run instead of waiting for the next external trigger.
            if (callsOk && smsOk && hitHarvestCap &&
                dao.pendingCountOf(PhoneEventKind.CALL) == 0 &&
                dao.pendingCountOf(PhoneEventKind.SMS) == 0
            ) {
                Scheduling.enqueueEventSync(applicationContext)
            }

            if (callsOk && smsOk) Result.success() else Result.retry()
        } catch (c: CancellationException) {
            // WorkManager stopping us is not an error; rethrow and reschedule.
            throw c
        } catch (t: Throwable) {
            Logx.e("Event sync failed", t)
            Result.retry()
        }
    }

    // ------------------------------------------------------------ harvesting

    /** Returns the highest CallLog._ID seen (or [afterId] untouched). */
    private suspend fun harvestCalls(
        dao: PhoneEventDao,
        deviceId: String,
        afterId: Long,
        enabled: Boolean,
    ): Long {
        if (!enabled || !StorageAccess.hasCallLog(applicationContext)) return afterId
        val matches = CallLogMatcher.since(applicationContext, afterId, MAX_BATCH)
        if (matches.size >= MAX_BATCH) hitHarvestCap = true
        if (matches.isEmpty()) return afterId

        val region = PhoneNormalizer.regionCode(applicationContext)
        val rows = matches.map { m ->
            val number = PhoneNormalizer.toE164(m.number, region)
                ?: m.number?.trim()?.takeIf { it.isNotEmpty() }
            // The cached name misses contacts added after the call; one
            // lookup fills it, and a contact name is what lets the server
            // resolve the caller to a real Person.
            val name = m.cachedName ?: CallLogMatcher.contactName(applicationContext, m.number)
            val payload = JSONObject()
                .put("call_log_id", m.callLogId)
                .put("started_at", TimeFmt.isoOffsetExact(m.startedAtMillis))
                .put("duration_seconds", m.durationSeconds.coerceAtLeast(0))
                .put("type", m.direction)
            if (number != null) payload.put("number", number)
            if (name != null) payload.put("contact_name", name)
            m.simSlot?.let { payload.put("sim_slot", it) }
            PhoneEventEntity(
                key = "$deviceId:call:${m.callLogId}",
                kind = PhoneEventKind.CALL,
                payloadJson = payload.toString(),
            )
        }
        dao.insertIgnore(rows)
        Logx.i("Harvested ${rows.size} call-log event(s)")
        return matches.maxOf { it.callLogId }
    }

    /** Returns the highest Sms._ID seen — including filtered-out senders. */
    private suspend fun harvestSms(
        dao: PhoneEventDao,
        deviceId: String,
        afterId: Long,
        mode: String,
    ): Long {
        if (mode == SmsMode.OFF || !StorageAccess.hasSms(applicationContext)) return afterId

        var maxSeen = afterId
        val rows = ArrayList<PhoneEventEntity>()
        var skipped = 0
        try {
            applicationContext.contentResolver.query(
                Telephony.Sms.Inbox.CONTENT_URI,
                arrayOf(
                    Telephony.Sms._ID,
                    Telephony.Sms.ADDRESS,
                    Telephony.Sms.BODY,
                    Telephony.Sms.DATE,
                    Telephony.Sms.SUBSCRIPTION_ID,
                ),
                "${Telephony.Sms._ID} > ?",
                arrayOf(afterId.toString()),
                "${Telephony.Sms._ID} ASC",
            )?.use { c ->
                val idIdx = c.getColumnIndexOrThrow(Telephony.Sms._ID)
                val addrIdx = c.getColumnIndexOrThrow(Telephony.Sms.ADDRESS)
                val bodyIdx = c.getColumnIndexOrThrow(Telephony.Sms.BODY)
                val dateIdx = c.getColumnIndexOrThrow(Telephony.Sms.DATE)
                val subIdx = c.getColumnIndex(Telephony.Sms.SUBSCRIPTION_ID)

                var seen = 0
                while (c.moveToNext() && seen < MAX_BATCH) {
                    seen++
                    if (seen == MAX_BATCH) hitHarvestCap = true
                    val smsId = c.getLong(idIdx)
                    if (smsId > maxSeen) maxSeen = smsId
                    val sender = c.getString(addrIdx)?.trim().orEmpty()
                    val body = c.getString(bodyIdx).orEmpty()
                    if (sender.isEmpty() || body.isEmpty()) {
                        skipped++
                        continue
                    }
                    if (mode == SmsMode.PAYMENTS && !PaymentSenders.isPayment(sender)) {
                        skipped++
                        continue
                    }
                    // take() counts UTF-16 units; never cut an emoji's
                    // surrogate pair in half — org.json would emit malformed
                    // UTF-8 the server may refuse.
                    var clipped = body.take(MAX_SMS_CHARS)
                    if (clipped.isNotEmpty() && clipped.last().isHighSurrogate()) {
                        clipped = clipped.dropLast(1)
                    }
                    val receivedAt = TimeFmt.isoOffsetExact(c.getLong(dateIdx))
                    val payload = JSONObject()
                        .put("sms_id", smsId)
                        .put("sender", sender)
                        .put("received_at", receivedAt)
                        .put("body", clipped)
                    slotOf(if (subIdx >= 0 && !c.isNull(subIdx)) c.getInt(subIdx) else -1)
                        ?.let { payload.put("sim_slot", it) }
                    rows.add(
                        PhoneEventEntity(
                            key = smsKey(deviceId, smsId, sender, receivedAt, clipped),
                            kind = PhoneEventKind.SMS,
                            payloadJson = payload.toString(),
                        )
                    )
                }
            }
        } catch (t: Throwable) {
            // Do not advance past rows we could not read; the harvested tail
            // is re-read next sweep and the queue's key absorbs the repeat.
            Logx.w("SMS harvest failed: ${t.message}")
            return afterId
        }
        if (rows.isNotEmpty() || skipped > 0) {
            if (rows.isNotEmpty()) dao.insertIgnore(rows)
            // Counts only — an SMS body or sender never reaches logcat.
            Logx.i("Harvested ${rows.size} SMS, filtered $skipped (mode=$mode)")
        }
        return maxSeen
    }

    // --------------------------------------------------------------- posting

    /** True when every pending batch of [kind] is settled with the server. */
    private suspend fun postPending(
        dao: PhoneEventDao,
        kind: String,
        baseUrl: String,
        deviceId: String,
    ): Boolean {
        while (true) {
            val batch = dao.nextBatch(kind, MAX_BATCH)
            if (batch.isEmpty()) return true

            val items = ArrayList<JSONObject>(batch.size)
            val unparseable = ArrayList<String>()
            for (row in batch) {
                try {
                    items.add(JSONObject(row.payloadJson))
                } catch (t: Throwable) {
                    unparseable.add(row.key)
                }
            }
            if (unparseable.isNotEmpty()) {
                // Can only be a bug in this app; park those rows and re-read.
                Logx.e("Parking ${unparseable.size} unparseable $kind payload(s)")
                dao.markFailed(unparseable, UploadState.FAILED_PERMANENT)
                continue
            }

            val outcome = if (kind == PhoneEventKind.CALL) {
                Graph.api.postCalls(baseUrl, deviceId, items)
            } else {
                Graph.api.postSms(baseUrl, deviceId, items)
            }
            when (outcome) {
                is EventPostOutcome.Delivered -> {
                    // 200 settles the whole batch: accepted, duplicates, and
                    // rejected alike (the server named its reason once, and
                    // re-posting a rejected event fails the same validation
                    // forever).
                    dao.markDone(batch.map { it.key })
                    Logx.i(
                        "Posted ${batch.size} $kind event(s): ${outcome.accepted} new, " +
                            "${outcome.duplicates} duplicate(s), ${outcome.rejected} rejected"
                    )
                    if (outcome.rejected > 0) {
                        Graph.prefs.setLastError(
                            "Server rejected ${outcome.rejected} $kind event(s); see server logs"
                        )
                    }
                }

                is EventPostOutcome.AuthFailed -> {
                    Graph.prefs.setAuthFailed(true)
                    Logx.w("Event post 401: ${outcome.detail}")
                    return false
                }

                is EventPostOutcome.Retry -> {
                    Logx.w("Event post will retry: ${outcome.detail}")
                    return false
                }

                is EventPostOutcome.Permanent -> {
                    // One parked batch, then stop: if the cause is the whole
                    // endpoint (a proxy rewriting errors, say), marching on
                    // would park every batch in the queue in one sweep.
                    Logx.e("Event post rejected for good: ${outcome.detail}")
                    dao.markFailed(batch.map { it.key }, UploadState.FAILED_PERMANENT)
                    Graph.prefs.setLastError("Phone events rejected: ${outcome.detail}")
                    return false
                }
            }
        }
    }

    // ------------------------------------------------------------------ util

    /**
     * The server's sms_event_key, minted identically here: the content hash
     * is load-bearing because Android's Sms._ID restarts after a wipe, so the
     * provider id alone could make a genuinely new message look old.
     */
    private fun smsKey(
        deviceId: String,
        smsId: Long,
        sender: String,
        receivedAtIso: String,
        body: String,
    ): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest("$sender|$receivedAtIso|$body".toByteArray(Charsets.UTF_8))
        // First 8 bytes = the server's hexdigest()[:16]. Built by hand so no
        // locale can ever localise a "digit" of the key.
        val hex = StringBuilder(16)
        for (i in 0 until 8) {
            val b = digest[i].toInt() and 0xff
            hex.append(HEX_DIGITS[b ushr 4]).append(HEX_DIGITS[b and 0x0f])
        }
        return "$deviceId:sms:$smsId:$hex"
    }

    /** Subscription id → SIM slot; best effort, null is fine (API 29+). */
    private fun slotOf(subId: Int): Int? {
        if (subId < 0) return null
        return try {
            SubscriptionManager.getSlotIndex(subId).takeIf { it >= 0 }
        } catch (t: Throwable) {
            null
        }
    }

    private companion object {
        const val HEX_DIGITS = "0123456789abcdef"

        /** The server refuses bigger batches; mirrored, never assumed. */
        const val MAX_BATCH = 200

        /** The server caps an SMS body at 4096 chars; clip before posting. */
        const val MAX_SMS_CHARS = 4096

        /** DONE rows are only a dedupe cache; a week is plenty. */
        const val KEEP_DONE_MILLIS = 7L * 24 * 60 * 60 * 1000
    }
}
