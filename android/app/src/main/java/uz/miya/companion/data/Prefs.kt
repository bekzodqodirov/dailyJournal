package uz.miya.companion.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import java.util.UUID

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "miya_prefs")

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
) {
    val serverConfigured: Boolean get() = serverUrl.isNotBlank()
}

class Prefs(private val context: Context) {

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
    )

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
