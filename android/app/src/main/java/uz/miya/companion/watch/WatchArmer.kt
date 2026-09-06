package uz.miya.companion.watch

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import uz.miya.companion.data.Prefs
import uz.miya.companion.discover.MediaStoreQuery
import uz.miya.companion.discover.OemCandidates
import uz.miya.companion.service.IngestService
import uz.miya.companion.util.Logx
import uz.miya.companion.util.StorageAccess
import uz.miya.companion.work.Scheduling
import java.io.File

/**
 * The single place that (re-)registers every observer. Called on app start, on
 * boot, on package replace, after the folder is picked, and after every
 * call-end burst.
 *
 * Re-arming on every call end is not belt-and-braces, it is load-bearing:
 * FileObserver reports nothing for a directory that did not exist when it was
 * registered, EVEN IF the directory appears later — and on a fresh phone the
 * OEM recordings folder does not exist until the first recorded call.
 */
class WatchArmer(
    private val context: Context,
    private val prefs: Prefs,
    private val scope: CoroutineScope,
) {

    // Strong references. A garbage-collected FileObserver silently stops
    // delivering events: works in debug, dies in release.
    private var fileObserver: RecordingFileObserver? = null
    private var ancestorObserver: RecordingFileObserver? = null

    private val mediaWatcher = MediaStoreWatcher(context) {
        scope.launch {
            if (mediaGenerationAdvanced()) Scheduling.enqueueBurstScan(context)
        }
    }

    private val callWatcher = CallStateWatcher(context) { onCallEnded() }

    @Volatile
    var watchedDirs: List<String> = emptyList()
        private set

    @Volatile
    var callTriggerActive: Boolean = false
        private set

    @Volatile
    private var started = false

    /** Idempotent: MiyaApp and BootReceiver both call it in the same process. */
    @Synchronized
    fun start() {
        if (started) {
            rearm()
            return
        }
        callTriggerActive = callWatcher.register()
        mediaWatcher.register()
        started = true
        rearm()
    }

    /**
     * ContentObserver.onChange carries no delta and fires for deletes too.
     * MediaStore generation numbers (API 30+) are the documented way to ask
     * "did anything actually get added?" — DATE_MODIFIED is not, because any
     * app can rewrite it and a wrong system clock breaks it. getVersion()
     * changing invalidates every stored generation number, so it is checked
     * first. On API 29 there are no generations and every notification is
     * treated as real.
     */
    private suspend fun mediaGenerationAdvanced(): Boolean {
        val current = MediaStoreQuery.generation(context) ?: return true
        val snapshot = prefs.snapshot()
        val advanced = snapshot.lastMediaVersion != current.first ||
            current.second > snapshot.lastMediaGeneration
        if (advanced) prefs.setMediaGeneration(current.first, current.second)
        return advanced
    }

    fun rearm() {
        scope.launch {
            val snapshot = prefs.snapshot()
            val folders = snapshot.folderRelativePath?.let { listOf(it) }
                ?: OemCandidates.ordered()
            armFileObservers(folders)
        }
    }

    /**
     * @Synchronized because `rearm()` launches into a shared scope and is
     * called concurrently from three different threads: the telephony
     * executor (`onCallEnded`), the FileObserver thread (the ancestor
     * callback), and the main thread (`runProbe` / `onFolderPicked`). Two
     * overlapping runs could have one call `stopObservers()` on the instance
     * the other had just registered, leaving `fileObserver` non-null but not
     * watching — precisely the silent death this class exists to prevent.
     */
    @Synchronized
    private fun armFileObservers(relativePaths: List<String>) {
        val root = try {
            Environment.getExternalStorageDirectory()
        } catch (t: Throwable) {
            Logx.w("No external storage root: ${t.message}")
            return
        }

        // inotify needs real read access to the path. Under scoped storage
        // File.isDirectory() returns false for /sdcard/Recordings/Call even on
        // a phone that records there perfectly well, and a watch on a path we
        // cannot read delivers nothing — so on a normal install `targets` is
        // empty and the ancestor branch is all that could run. Registering
        // that branch anyway used to walk up to the first "existing"
        // directory, which is the external storage ROOT, and then fired a
        // burst scan plus a full re-arm for every screenshot and download.
        val canWatch = StorageAccess.allFiles()

        val targets = LinkedHashSet<File>()
        val ancestors = LinkedHashSet<File>()

        if (canWatch) {
            for (relative in relativePaths) {
                val dir = File(root, relative.trim('/'))
                if (dir.isDirectory) {
                    targets.add(dir)
                } else {
                    // Watch the nearest EXISTING ancestor for the child
                    // appearing — but never the storage root itself.
                    var parent: File? = dir.parentFile
                    while (parent != null && !parent.isDirectory) parent = parent.parentFile
                    if (parent != null &&
                        parent.absolutePath.startsWith(root.absolutePath) &&
                        parent.absolutePath != root.absolutePath
                    ) {
                        ancestors.add(parent)
                    }
                }
            }
        }

        stopObservers()

        if (targets.isNotEmpty()) {
            fileObserver = RecordingFileObserver(
                targets.toList(),
                RecordingFileObserver.MASK_FILES,
            ) { _, path ->
                Logx.d("FileObserver: finished write ${Logx.redactName(path)}")
                Scheduling.enqueueBurstScan(context)
            }.also { runCatching { it.startWatching() } }
        }

        if (ancestors.isNotEmpty()) {
            ancestorObserver = RecordingFileObserver(
                ancestors.toList(),
                RecordingFileObserver.MASK_DIRS,
            ) { _, path ->
                Logx.d("FileObserver: new entry ${Logx.redactName(path)} in ancestor; re-arming")
                Scheduling.enqueueBurstScan(context)
                rearm()
            }.also { runCatching { it.startWatching() } }
        }

        watchedDirs = targets.map { it.absolutePath }
        if (!canWatch) {
            Logx.i(
                "inotify unavailable without All files access; relying on call-end, " +
                    "MediaStore and the 15-minute sweep"
            )
        } else {
            Logx.i("Armed on ${targets.size} folder(s), ${ancestors.size} ancestor(s)")
        }
    }

    private fun stopObservers() {
        runCatching { fileObserver?.stopWatching() }
        runCatching { ancestorObserver?.stopWatching() }
        fileObserver = null
        ancestorObserver = null
    }

    /**
     * Call ended. Try a foreground burst first — it is the one path that beats
     * an OEM cleaner to a freshly written file. Starting an FGS from the
     * background is only allowed via an enumerated exemption (chiefly the
     * battery-optimisation exemption), so a refusal is expected, not
     * exceptional, and falls back to WorkManager.
     */
    fun onCallEnded() {
        // Re-arm first: the folder may have just been created by this very call.
        rearm()

        // Do not even attempt the foreground start when the platform is known
        // to refuse it. From Android 12 a background FGS start needs an
        // enumerated exemption, and the battery-optimisation exemption is the
        // one this app asks for; without it the start is refused — and because
        // startForegroundService() was already called, several OEM builds
        // still deliver the RemoteServiceException ("did not then call
        // Service.startForeground()") for the five-second window even though
        // the service stopped itself. WorkManager is the correct path there.
        if (!canStartForegroundFromBackground()) {
            Logx.d("Not exempt from battery optimisation; call-end burst goes to WorkManager")
            Scheduling.enqueueBurstScan(context)
            return
        }

        try {
            ContextCompat.startForegroundService(
                context,
                Intent(context, IngestService::class.java)
                    .setAction(IngestService.ACTION_SCAN),
            )
        } catch (t: Throwable) {
            // ForegroundServiceStartNotAllowedException (API 31+) is an
            // IllegalStateException; SecurityException is also possible.
            Logx.w("Foreground burst refused (${t.javaClass.simpleName}); using WorkManager")
            Scheduling.enqueueBurstScan(context)
        }
    }

    private fun canStartForegroundFromBackground(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
        val pm = context.getSystemService(PowerManager::class.java) ?: return false
        return try {
            pm.isIgnoringBatteryOptimizations(context.packageName)
        } catch (t: Throwable) {
            false
        }
    }

    @Synchronized
    fun stop() {
        stopObservers()
        mediaWatcher.unregister()
        callWatcher.unregister()
        started = false
    }
}
