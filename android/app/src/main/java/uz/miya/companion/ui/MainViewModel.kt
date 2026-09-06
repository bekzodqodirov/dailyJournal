package uz.miya.companion.ui

import android.Manifest
import android.app.Application
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import uz.miya.companion.Graph
import uz.miya.companion.data.PrefsSnapshot
import uz.miya.companion.data.UploadEntity
import uz.miya.companion.data.UploadState
import uz.miya.companion.discover.FolderProbe
import uz.miya.companion.discover.OemCandidates
import uz.miya.companion.discover.ProbeVerdict
import uz.miya.companion.discover.SafScanner
import uz.miya.companion.net.UploadApi
import uz.miya.companion.util.Logx
import uz.miya.companion.util.StorageAccess
import uz.miya.companion.work.Scheduling

data class UiState(
    val prefs: PrefsSnapshot? = null,
    val rows: List<UploadEntity> = emptyList(),
    val queueDepth: Int = 0,
    val lastUploadAt: Long? = null,
    val health: List<HealthItem> = emptyList(),
    val tokenSet: Boolean = false,
    val tokenMasked: String = "",
    val busy: Boolean = false,
    val message: String? = null,
    val probeVerdict: String? = null,
    val watchedDirs: List<String> = emptyList(),
    val callTriggerActive: Boolean = false,
) {
    val failedRows: List<UploadEntity>
        get() = rows.filter {
            it.state == UploadState.FAILED_PERMANENT || it.state == UploadState.FAILED_PRECONDITION
        }
}

private data class LocalState(
    val health: List<HealthItem> = emptyList(),
    val tokenSet: Boolean = false,
    val tokenMasked: String = "",
    val busy: Boolean = false,
    val message: String? = null,
    val watchedDirs: List<String> = emptyList(),
    val callTriggerActive: Boolean = false,
)

class MainViewModel(app: Application) : AndroidViewModel(app) {

    private val context: Context get() = getApplication()
    private val local = MutableStateFlow(LocalState())

    init {
        Graph.init(app)
        refresh()
    }

    val state: StateFlow<UiState> = combine(
        Graph.prefs.flow,
        Graph.repository.recent,
        Graph.repository.queueDepth,
        Graph.repository.lastUploadAt,
        local,
    ) { prefs, rows, depth, lastUpload, localState ->
        UiState(
            prefs = prefs,
            rows = rows,
            queueDepth = depth,
            lastUploadAt = lastUpload,
            health = localState.health,
            tokenSet = localState.tokenSet,
            tokenMasked = localState.tokenMasked,
            busy = localState.busy,
            message = localState.message,
            probeVerdict = prefs.probeVerdict,
            watchedDirs = localState.watchedDirs,
            callTriggerActive = localState.callTriggerActive,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), UiState())

    // ---------------------------------------------------------------- health

    /**
     * Re-checked on EVERY launch and every resume, never once at onboarding:
     * Android auto-revokes permissions for unused apps, and Samsung and Xiaomi
     * reset their own battery toggles on OS updates.
     */
    fun refresh() {
        viewModelScope.launch {
            val snapshot = Graph.prefs.snapshot()
            // viewModelScope dispatches on Dispatchers.Main.immediate, and all
            // three of these block: the first tokenStore call loads a
            // SharedPreferences file from disk, masked() does an AndroidKeyStore
            // lookup plus an AES-GCM decrypt, and computeHealth() queries the
            // package manager and PowerManager. refresh() runs in init, on
            // every onResume and after every permission result, so on a cold
            // start this was a StrictMode violation and a realistic ANR — on
            // the onboarding screen, where the owner is already frustrated.
            val computed = withContext(Dispatchers.IO) {
                Triple(
                    computeHealth(snapshot),
                    Graph.tokenStore.hasToken(),
                    Graph.tokenStore.masked(),
                )
            }
            local.value = local.value.copy(
                health = computed.first,
                tokenSet = computed.second,
                tokenMasked = computed.third,
                watchedDirs = Graph.watchArmer.watchedDirs,
                callTriggerActive = Graph.watchArmer.callTriggerActive,
            )
        }
    }

    private fun computeHealth(prefs: PrefsSnapshot): List<HealthItem> {
        val items = ArrayList<HealthItem>()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val ok = StorageAccess.granted(context, Manifest.permission.POST_NOTIFICATIONS)
            items += HealthItem(
                title = "Notifications",
                ok = ok,
                detail = if (ok) "Granted." else
                    "Denied. The foreground-service notification is suppressed, which makes " +
                        "MIYA invisible and far more likely to be killed.",
                action = HealthAction.NOTIFICATION_PERMISSION,
                actionLabel = "Grant",
            )
        }

        val media = StorageAccess.hasMediaAudio(context)
        items += HealthItem(
            title = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                "Music & audio access" else "Storage access",
            ok = media,
            detail = if (media) "Granted." else
                "Denied. Without it MIYA cannot read the files your dialer writes.",
            action = HealthAction.MEDIA_PERMISSION,
            actionLabel = "Grant",
        )

        val phoneState = StorageAccess.granted(context, Manifest.permission.READ_PHONE_STATE)
        items += HealthItem(
            title = "Phone state (call-end trigger)",
            ok = phoneState,
            detail = if (phoneState) "Granted. New recordings are picked up seconds after hang-up."
            else "Denied. MIYA falls back to a 15-minute sweep, which some phones defer for hours.",
            action = HealthAction.PHONE_STATE_PERMISSION,
            actionLabel = "Grant",
            critical = false,
        )

        val callLog = StorageAccess.granted(context, Manifest.permission.READ_CALL_LOG)
        items += HealthItem(
            title = "Call log (optional)",
            ok = callLog,
            detail = if (callLog) {
                "Granted. Direction, exact start time, duration and contact name are real."
            } else {
                "Not granted. READ_CALL_LOG is hard-restricted on Android 10+ and a sideloaded " +
                    "APK often can never hold it. MIYA works anyway: it falls back to the " +
                    "filename and file time, direction becomes \"unknown\", and the server " +
                    "accepts all of that."
            },
            action = HealthAction.CALL_LOG_PERMISSION,
            actionLabel = "Try anyway",
            critical = false,
        )

        val tree = prefs.treeUri?.let(Uri::parse)
        val treeOk = tree != null && SafScanner.stillGranted(context, tree)
        // A confirmed folder AND a way to read it. All-files access on its own
        // is no longer enough, because an unconfirmed folder is never scanned:
        // scanning every OEM candidate root uploaded the owner's voice memos
        // and ringtones along with their calls.
        val folderOk = treeOk ||
            (prefs.folderRelativePath != null && (media || StorageAccess.allFiles()))
        items += HealthItem(
            title = "Recordings folder",
            ok = folderOk,
            detail = when {
                treeOk -> "Folder grant held: ${tree?.lastPathSegment ?: prefs.treeUri}"
                prefs.folderRelativePath != null -> "Watching /${prefs.folderRelativePath}"
                StorageAccess.allFiles() ->
                    "All-files access is on, but nothing is uploaded until a folder is " +
                        "confirmed — tap Auto-detect or Pick by hand."
                else -> "No folder confirmed yet. Record one test call, then run detection."
            },
            action = HealthAction.PICK_FOLDER,
            actionLabel = "Pick folder",
        )

        val pm = context.getSystemService(PowerManager::class.java)
        val batteryOk = pm?.isIgnoringBatteryOptimizations(context.packageName) ?: false
        items += HealthItem(
            title = "Battery optimisation disabled",
            ok = batteryOk,
            detail = if (batteryOk) "Exempt. Doze will not defer the queue, and MIYA may start " +
                "its short foreground burst from the background."
            else "Not exempt. Uploads will be deferred by Doze, and the call-end burst cannot " +
                "start from the background.",
            action = HealthAction.BATTERY,
            actionLabel = "Fix",
        )

        val cleartextHost = UploadApi.cleartextBlockedHost(prefs.serverUrl)
        val serverOk = prefs.serverConfigured && Graph.tokenStore.hasToken() &&
            !prefs.authFailed && cleartextHost == null
        items += HealthItem(
            title = "Server and token",
            ok = serverOk,
            detail = when {
                !prefs.serverConfigured -> "No server URL set."
                !Graph.tokenStore.hasToken() -> "No bearer token saved."
                prefs.authFailed -> "The server rejected the token (401). Uploads are stopped."
                // Asked of the platform, not guessed: this is the same policy
                // OkHttp consults before it opens the socket, so it is the
                // difference between "the tunnel is down" and "this build will
                // never talk to that address in plaintext".
                cleartextHost != null -> UploadApi.cleartextAdvice(cleartextHost)
                else -> "Configured: ${prefs.serverUrl}"
            },
            action = HealthAction.SERVER_SETTINGS,
            actionLabel = "Open settings",
        )

        items += HealthItem(
            title = "OEM background limits",
            ok = true,
            detail = "Samsung and Xiaomi reset autostart and battery toggles on OS updates. " +
                "Re-check this list after every system update.",
            action = HealthAction.OEM,
            actionLabel = "Checklist",
            critical = false,
        )

        return items
    }

    // ------------------------------------------------------------- detection

    fun runProbe() {
        viewModelScope.launch {
            local.value = local.value.copy(busy = true, message = null)
            // Up to ~15 ContentResolver round trips into MediaProvider, plus
            // File.listFiles() per candidate in all-files mode. Never on the
            // main thread.
            val verdict = withContext(Dispatchers.IO) {
                FolderProbe.detect(context, StorageAccess.hasMediaAudio(context))
            }
            when (verdict) {
                is ProbeVerdict.Found -> {
                    Graph.prefs.setFolder(verdict.relativePath)
                    Graph.prefs.setProbeVerdict(verdict.label())
                    Graph.watchArmer.rearm()
                    Scheduling.enqueueImmediateScan(context)
                    local.value = local.value.copy(
                        busy = false,
                        message = "Found recordings in /${verdict.relativePath}",
                    )
                }
                ProbeVerdict.GoogleDialerUnreachable -> {
                    Graph.prefs.setProbeVerdict(verdict.label())
                    local.value = local.value.copy(
                        busy = false,
                        message = "This phone uses Google Dialer. Its recordings are in " +
                            "app-private storage that no app can read. Use Share to MIYA " +
                            "from the Phone app instead.",
                    )
                }
                ProbeVerdict.NoRecorder -> {
                    Graph.prefs.setProbeVerdict(verdict.label())
                    local.value = local.value.copy(
                        busy = false,
                        message = "No recordings found. Turn on call recording in your dialer, " +
                            "make one 10-second test call, record it, then try again. " +
                            "The folder does not exist until the first recording.",
                    )
                }
                ProbeVerdict.NeedsPermission -> {
                    local.value = local.value.copy(
                        busy = false,
                        message = "Grant music & audio access first.",
                    )
                }
            }
            refresh()
        }
    }

    fun onFolderPicked(treeUri: Uri) {
        viewModelScope.launch {
            if (SafScanner.persist(context, treeUri)) {
                Graph.prefs.setTreeUri(treeUri.toString())
                // Best-effort: derive a relative path so the FileObserver has
                // something real to watch too. SAF alone gives no inotify.
                treeUri.lastPathSegment
                    ?.substringAfter(':', "")
                    ?.takeIf { it.isNotBlank() }
                    ?.let { Graph.prefs.setFolder("$it/") }
                Graph.watchArmer.rearm()
                Scheduling.enqueueImmediateScan(context)
                local.value = local.value.copy(message = "Folder saved.")
            } else {
                local.value = local.value.copy(message = "Could not keep access to that folder.")
            }
            refresh()
        }
    }

    fun folderHint(): String? = OemCandidates.ordered().firstOrNull()

    // ---------------------------------------------------------------- server

    /**
     * Validates before it stores, and RELEASES the queue after it stores.
     *
     * Both halves were missing. An unparseable URL used to be saved happily and
     * then blew up inside HttpUrl in a worker, where the failure was invisible.
     * And nothing re-queued the rows that had already failed for want of a
     * configuration: recordings found before this screen was ever reached sat
     * in FAILED_PRECONDITION, and rows failed by the 401 latch sat in
     * FAILED_PERMANENT, so the owner would paste a corrected token, see a green
     * Health screen, and watch a queue that still never moved.
     */
    fun saveServer(url: String, token: String?) {
        viewModelScope.launch {
            val trimmed = url.trim().trimEnd('/')
            if (trimmed.isNotBlank() && !UploadApi.looksLikeUrl(trimmed)) {
                local.value = local.value.copy(
                    message = "That is not a usable server URL. It must look like " +
                        "http://vps.tailnet-name.ts.net:8000 — scheme included.",
                )
                return@launch
            }

            Graph.prefs.setServerUrl(trimmed)

            var message = "Saved."
            if (!token.isNullOrBlank()) {
                val stored = withContext(Dispatchers.IO) { Graph.tokenStore.save(token.trim()) }
                if (stored) {
                    Graph.prefs.setAuthFailed(false)
                } else {
                    message = "Could not store the token securely on this device. " +
                        "The KeyStore refused the key; nothing was saved."
                }
            }

            if (message == "Saved.") {
                UploadApi.cleartextBlockedHost(trimmed)?.let { host ->
                    message = "Saved, but uploads will fail. " + UploadApi.cleartextAdvice(host)
                }
            }

            // The configuration just changed, so everything parked for want of
            // one gets another chance — including the rows the 401 latch failed.
            Graph.repository.retryAllFailed()

            local.value = local.value.copy(message = message)
            refresh()
        }
    }

    fun clearToken() {
        viewModelScope.launch {
            withContext(Dispatchers.IO) { Graph.tokenStore.clear() }
            refresh()
        }
    }

    /**
     * Hits /health (reachability) and then /v1/recordings/probe with an empty
     * batch (auth). Both, because a green tunnel with a bad token looks
     * identical to a working setup until the first real upload fails.
     */
    fun testConnection() {
        viewModelScope.launch {
            local.value = local.value.copy(busy = true, message = null)
            val snapshot = Graph.prefs.snapshot()
            if (!UploadApi.looksLikeUrl(snapshot.serverUrl)) {
                local.value = local.value.copy(
                    busy = false,
                    message = "Server URL must be a full http:// or https:// address, " +
                        "e.g. http://vps.tailnet-name.ts.net:8000",
                )
                return@launch
            }
            // Ask the platform first. Otherwise the cleartext refusal arrives
            // as an IOException and gets reported as "is the tunnel up?", which
            // sends the owner to debug a tunnel that is working fine.
            UploadApi.cleartextBlockedHost(snapshot.serverUrl)?.let { host ->
                local.value = local.value.copy(
                    busy = false,
                    message = UploadApi.cleartextAdvice(host),
                )
                return@launch
            }
            val health = Graph.api.health(snapshot.serverUrl)
            if (health.isFailure) {
                local.value = local.value.copy(
                    busy = false,
                    message = "Cannot reach ${snapshot.serverUrl}: " +
                        "${health.exceptionOrNull()?.message}. Is the tunnel up?",
                )
                return@launch
            }
            val probe = Graph.api.probe(snapshot.serverUrl, emptyList(), emptyList())
            if (probe.isFailure) {
                val error = probe.exceptionOrNull()
                val isAuth = error is UploadApi.AuthException
                if (isAuth) Graph.prefs.setAuthFailed(true)
                local.value = local.value.copy(
                    busy = false,
                    message = if (isAuth) "Reachable, but the token was rejected (401)."
                    else "Reachable, but /v1/recordings/probe failed: ${error?.message}",
                )
            } else {
                Graph.prefs.setAuthFailed(false)
                // Proven reachable and authorised: release everything that was
                // parked while it was not.
                Graph.repository.retryAllFailed()
                local.value = local.value.copy(
                    busy = false,
                    message = "Connection and token are good. Anything that was blocked " +
                        "has been re-queued.",
                )
            }
            refresh()
        }
    }

    // ----------------------------------------------------------------- queue

    fun scanNow() {
        Scheduling.enqueueImmediateScan(context)
        local.value = local.value.copy(message = "Scanning…")
    }

    fun retry(sha: String) {
        viewModelScope.launch {
            Graph.prefs.setAuthFailed(false)
            Graph.repository.retryNow(sha)
            local.value = local.value.copy(message = "Retrying.")
        }
    }

    fun retryAll() {
        viewModelScope.launch {
            Graph.repository.retryAllFailed()
            local.value = local.value.copy(message = "Everything re-queued.")
            refresh()
        }
    }

    // These return Unit, not Job, so they can be passed straight to a Compose
    // (Boolean) -> Unit callback as a method reference.
    fun setWifiOnly(value: Boolean) {
        viewModelScope.launch { Graph.prefs.setWifiOnly(value) }
    }

    fun setDeleteAfterUpload(value: Boolean) {
        viewModelScope.launch { Graph.prefs.setDeleteAfterUpload(value) }
    }

    fun setLanguageHint(value: String) {
        viewModelScope.launch { Graph.prefs.setLanguageHint(value) }
    }

    fun setMinDuration(seconds: Int) {
        viewModelScope.launch { Graph.prefs.setMinDurationSeconds(seconds.coerceIn(0, 600)) }
    }

    fun completeOnboarding() {
        viewModelScope.launch {
            Graph.prefs.setOnboardingComplete(true)
            Graph.watchArmer.start()
            Scheduling.ensurePeriodicScan(context)
            Scheduling.enqueueImmediateScan(context)
            refresh()
        }
    }

    fun clearMessage() {
        local.value = local.value.copy(message = null)
    }

    fun logSnapshot() {
        Logx.i("Watched dirs: ${Graph.watchArmer.watchedDirs}")
    }
}
