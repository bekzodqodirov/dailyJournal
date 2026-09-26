package uz.miya.companion.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.core.stringSetPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import java.util.UUID

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "miya_prefs")

/**
 * The three SMS upload modes (build step 6). "payments" is the default: only
 * messages whose sender is on the payment allow-list leave the phone. The
 * server re-checks the sender either way — the client filter only limits what
 * leaves the phone, it is not the security boundary.
 */
object SmsMode {
    const val OFF = "off"
    const val PAYMENTS = "payments"
    const val ALL = "all"
}

/**
 * Everything except the bearer token. The token lives in [TokenStore], encrypted
 * with a KeyStore-held key, because DataStore files are plain XML/proto on disk.
 */
data class PrefsSnapshot(
    val serverUrl: String,
    val deviceId: String,
    val folderRelativePath: String?,
    val treeUri: String?,
    val wifiOnly: Boolean,
    val deleteAfterUpload: Boolean,
    val languageHint: String?,
    val minDurationSeconds: Int,
    val onboardingComplete: Boolean,
    val authFailed: Boolean,
    val lastMediaGeneration: Long,
    val lastMediaVersion: String?,
    val lastScanAt: Long,
    val lastErrorAt: Long,
    val lastError: String?,
    val probeVerdict: String?,
    // ---- phone events (build step 6) ----------------------------------
    /** High-water CallLog._ID; advanced only after the server answered 200. */
    val lastCallLogId: Long,
    /** High-water Sms._ID; same discipline. */
    val lastSmsId: Long,
    val uploadCallLog: Boolean,
    /** One of [SmsMode]. */
    val smsMode: String,
    // ---- payment-app notifications (WP-64) ----------------------------
    /** Packages whose notification text is read; empty until the owner ticks. */
    val paymentAppPackages: Set<String> = emptySet(),
    /** Package NAMES that posted anything, newest last, at most 50. */
    val seenPackages: List<String> = emptyList(),
    /** Packages the owner unticked: never auto-ticked again. */
    val untickedPackages: Set<String> = emptySet(),
    val listenerConnectedAt: Long? = null,
    val lastPaymentNotificationAt: Long? = null,
    val paymeAutoTicked: Boolean = false,
    // ---- first SMS import (WP-65) -------------------------------------
    /** How far back the first harvest reaches; 0 = from now. */
    val smsBackfillDays: Int = 30,
    /** The cutoff frozen at the first harvest; null until then. */
    val smsImportFromMs: Long? = null,
) {
    val serverConfigured: Boolean get() = serverUrl.isNotBlank()
}

class Prefs(private val context: Context) {

    private companion object {
        const val SEEN_CAP = 50
    }

    private object K {
        val SERVER_URL = stringPreferencesKey("server_url")
        val DEVICE_ID = stringPreferencesKey("device_id")
        val FOLDER = stringPreferencesKey("folder_relative_path")
        val TREE_URI = stringPreferencesKey("tree_uri")
        val WIFI_ONLY = booleanPreferencesKey("wifi_only")
        val DELETE_AFTER = booleanPreferencesKey("delete_after_upload")
        val LANGUAGE = stringPreferencesKey("language_hint")
        val MIN_DURATION = longPreferencesKey("min_duration_seconds")
        val ONBOARDED = booleanPreferencesKey("onboarding_complete")
        val AUTH_FAILED = booleanPreferencesKey("auth_failed")
        val MEDIA_GENERATION = longPreferencesKey("media_generation")
        val MEDIA_VERSION = stringPreferencesKey("media_version")
        val LAST_SCAN = longPreferencesKey("last_scan_at")
        val LAST_ERROR_AT = longPreferencesKey("last_error_at")
        val LAST_ERROR = stringPreferencesKey("last_error")
        val PROBE_VERDICT = stringPreferencesKey("probe_verdict")
        val LAST_CALL_LOG_ID = longPreferencesKey("last_call_log_id")
        val LAST_SMS_ID = longPreferencesKey("last_sms_id")
        val UPLOAD_CALL_LOG = booleanPreferencesKey("upload_call_log")
        val SMS_MODE = stringPreferencesKey("sms_mode")
        val PAYMENT_APP_PACKAGES = stringSetPreferencesKey("payment_app_packages")
        // DataStore has no ordered list; "<epoch millis>|<package>" keeps order.
        val SEEN_NOTIFYING_PACKAGES = stringSetPreferencesKey("seen_notifying_packages")
        val UNTICKED_PACKAGES = stringSetPreferencesKey("unticked_packages")
        val LISTENER_CONNECTED_AT = longPreferencesKey("listener_connected_at")
        val LAST_PAYMENT_NOTIFICATION_AT = longPreferencesKey("last_payment_notification_at")
        val PAYME_AUTO_TICKED = booleanPreferencesKey("payme_auto_ticked")
        val SMS_BACKFILL_DAYS = longPreferencesKey("sms_backfill_days")
        val SMS_IMPORT_FROM_MS = longPreferencesKey("sms_import_from_ms")
    }

    val flow: Flow<PrefsSnapshot> = context.dataStore.data.map { it.toSnapshot() }

    private fun Preferences.toSnapshot() = PrefsSnapshot(
        serverUrl = this[K.SERVER_URL].orEmpty(),
        deviceId = this[K.DEVICE_ID].orEmpty(),
        folderRelativePath = this[K.FOLDER],
        treeUri = this[K.TREE_URI],
        wifiOnly = this[K.WIFI_ONLY] ?: false,
        deleteAfterUpload = this[K.DELETE_AFTER] ?: false,
        languageHint = this[K.LANGUAGE],
        minDurationSeconds = (this[K.MIN_DURATION] ?: 4L).toInt(),
        onboardingComplete = this[K.ONBOARDED] ?: false,
        authFailed = this[K.AUTH_FAILED] ?: false,
        lastMediaGeneration = this[K.MEDIA_GENERATION] ?: 0L,
        lastMediaVersion = this[K.MEDIA_VERSION],
        lastScanAt = this[K.LAST_SCAN] ?: 0L,
        lastErrorAt = this[K.LAST_ERROR_AT] ?: 0L,
        lastError = this[K.LAST_ERROR],
        probeVerdict = this[K.PROBE_VERDICT],
        lastCallLogId = this[K.LAST_CALL_LOG_ID] ?: 0L,
        lastSmsId = this[K.LAST_SMS_ID] ?: 0L,
        uploadCallLog = this[K.UPLOAD_CALL_LOG] ?: true,
        smsMode = this[K.SMS_MODE] ?: SmsMode.PAYMENTS,
        paymentAppPackages = this[K.PAYMENT_APP_PACKAGES] ?: emptySet(),
        seenPackages = seenOrdered(this[K.SEEN_NOTIFYING_PACKAGES]),
        untickedPackages = this[K.UNTICKED_PACKAGES] ?: emptySet(),
        listenerConnectedAt = this[K.LISTENER_CONNECTED_AT],
        lastPaymentNotificationAt = this[K.LAST_PAYMENT_NOTIFICATION_AT],
        paymeAutoTicked = this[K.PAYME_AUTO_TICKED] ?: false,
        smsBackfillDays = (this[K.SMS_BACKFILL_DAYS] ?: 30L).toInt(),
        smsImportFromMs = this[K.SMS_IMPORT_FROM_MS],
    )

    private fun seenOrdered(raw: Set<String>?): List<String> =
        raw.orEmpty()
            .mapNotNull { entry ->
                val cut = entry.indexOf('|')
                if (cut <= 0) null else entry.substring(0, cut).toLongOrNull()?.let {
                    it to entry.substring(cut + 1)
                }
            }
            .sortedBy { it.first }
            .map { it.second }

    suspend fun snapshot(): PrefsSnapshot = flow.first()

    /**
     * The device_id is generated once and never changes; it is half of the
     * call_id the server dedupes on, so regenerating it would silently defeat
     * the strongest dedupe predicate.
     */
    suspend fun deviceId(): String {
        val existing = context.dataStore.data.first()[K.DEVICE_ID]
        if (!existing.isNullOrBlank()) return existing
        val fresh = UUID.randomUUID().toString()
        context.dataStore.edit { it[K.DEVICE_ID] = fresh }
        return fresh
    }

    suspend fun setServerUrl(value: String) = update { it[K.SERVER_URL] = value.trim().trimEnd('/') }
    suspend fun setFolder(relativePath: String?) = update {
        if (relativePath == null) it.remove(K.FOLDER) else it[K.FOLDER] = relativePath
    }
    suspend fun setTreeUri(value: String?) = update {
        if (value == null) it.remove(K.TREE_URI) else it[K.TREE_URI] = value
    }
    suspend fun setWifiOnly(value: Boolean) = update { it[K.WIFI_ONLY] = value }
    suspend fun setDeleteAfterUpload(value: Boolean) = update { it[K.DELETE_AFTER] = value }
    suspend fun setLanguageHint(value: String?) = update {
        if (value.isNullOrBlank()) it.remove(K.LANGUAGE) else it[K.LANGUAGE] = value.trim()
    }
    suspend fun setMinDurationSeconds(value: Int) = update { it[K.MIN_DURATION] = value.toLong() }
    suspend fun setOnboardingComplete(value: Boolean) = update { it[K.ONBOARDED] = value }
    suspend fun setAuthFailed(value: Boolean) = update { it[K.AUTH_FAILED] = value }
    suspend fun setProbeVerdict(value: String?) = update {
        if (value == null) it.remove(K.PROBE_VERDICT) else it[K.PROBE_VERDICT] = value
    }

    // ---- phone events (build step 6) --------------------------------------

    /**
     * The high-water marks advance ONLY after the server answered 200 for
     * everything harvested below them; EventSyncWorker owns that discipline.
     * They only ever move forward — a concurrent reset to zero (a "re-send
     * everything" someday) should not be silently undone by a late worker.
     */
    suspend fun setLastCallLogId(value: Long) = update {
        if (value > (it[K.LAST_CALL_LOG_ID] ?: 0L)) it[K.LAST_CALL_LOG_ID] = value
    }
    suspend fun setLastSmsId(value: Long) = update {
        if (value > (it[K.LAST_SMS_ID] ?: 0L)) it[K.LAST_SMS_ID] = value
    }
    suspend fun setUploadCallLog(value: Boolean) = update { it[K.UPLOAD_CALL_LOG] = value }
    suspend fun setSmsMode(value: String) = update {
        it[K.SMS_MODE] = when (value) {
            SmsMode.OFF, SmsMode.PAYMENTS, SmsMode.ALL -> value
            else -> SmsMode.PAYMENTS
        }
    }

    // ---- payment-app notifications (WP-64) --------------------------------

    /**
     * Remember that [pkg] posted a notification — its name only, never its
     * content — so the chooser can list it. Capped at the newest 50.
     */
    suspend fun noteSeenPackage(pkg: String) = update {
        val now = System.currentTimeMillis()
        val kept = (it[K.SEEN_NOTIFYING_PACKAGES] ?: emptySet())
            .filterNot { entry -> entry.substringAfter('|') == pkg }
            .sortedBy { entry -> entry.substringBefore('|').toLongOrNull() ?: 0L }
            .takeLast(SEEN_CAP - 1)
        it[K.SEEN_NOTIFYING_PACKAGES] = (kept + "$now|$pkg").toSet()
    }

    /** The owner's tick; an untick is remembered so Payme is not re-added. */
    suspend fun setPaymentAppTicked(pkg: String, ticked: Boolean) = update {
        val allow = (it[K.PAYMENT_APP_PACKAGES] ?: emptySet()).toMutableSet()
        val unticked = (it[K.UNTICKED_PACKAGES] ?: emptySet()).toMutableSet()
        if (ticked) {
            allow += pkg
            unticked -= pkg
        } else {
            allow -= pkg
            unticked += pkg
        }
        it[K.PAYMENT_APP_PACKAGES] = allow
        it[K.UNTICKED_PACKAGES] = unticked
        it[K.PAYME_AUTO_TICKED] = false
    }

    /** Payme, recognised by its label on first sight (never by package id). */
    suspend fun autoTickPayment(pkg: String) = update {
        it[K.PAYMENT_APP_PACKAGES] = (it[K.PAYMENT_APP_PACKAGES] ?: emptySet()) + pkg
        it[K.PAYME_AUTO_TICKED] = true
    }

    suspend fun setListenerConnectedAt(value: Long?) = update {
        if (value == null) it.remove(K.LISTENER_CONNECTED_AT)
        else it[K.LISTENER_CONNECTED_AT] = value
    }

    suspend fun markPaymentNotification() = update {
        it[K.LAST_PAYMENT_NOTIFICATION_AT] = System.currentTimeMillis()
    }

    // ---- first SMS import (WP-65) -------------------------------------------

    suspend fun setSmsBackfillDays(days: Int) = update { it[K.SMS_BACKFILL_DAYS] = days.toLong() }

    /**
     * Freeze the first-import cutoff ONCE. Recomputing "now" on every sweep
     * would move the cutoff forward forever while no SMS falls inside it.
     * Returns the frozen value, whoever set it.
     */
    suspend fun freezeSmsImportFrom(value: Long): Long {
        var frozen = value
        update {
            val existing = it[K.SMS_IMPORT_FROM_MS]
            if (existing == null) it[K.SMS_IMPORT_FROM_MS] = value else frozen = existing
        }
        return frozen
    }

    suspend fun setMediaGeneration(version: String, generation: Long) = update {
        it[K.MEDIA_VERSION] = version
        it[K.MEDIA_GENERATION] = generation
    }

    suspend fun markScanned() = update { it[K.LAST_SCAN] = System.currentTimeMillis() }

    suspend fun setLastError(message: String?) = update {
        if (message == null) {
            it.remove(K.LAST_ERROR)
            it.remove(K.LAST_ERROR_AT)
        } else {
            it[K.LAST_ERROR] = message
            it[K.LAST_ERROR_AT] = System.currentTimeMillis()
        }
    }

    // The DataStore extension takes a SUSPEND transform, so this parameter must
    // be declared suspend too — a plain (T) -> Unit value is not assignable to
    // a suspend (T) -> Unit parameter, only a lambda literal is.
    private suspend fun update(
        block: suspend (androidx.datastore.preferences.core.MutablePreferences) -> Unit,
    ) {
        context.dataStore.edit(block)
    }
}
