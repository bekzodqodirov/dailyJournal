package uz.miya.companion.watch

import android.content.Context
import android.content.Intent
import android.os.Environment
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import uz.miya.companion.data.Prefs
import uz.miya.companion.discover.MediaStoreQuery
import uz.miya.companion.discover.OemCandidates
import uz.miya.companion.service.IngestService
import uz.miya.companion.util.Logx
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

    private fun armFileObservers(relativePaths: List<String>) {
        val root = try {
            Environment.getExternalStorageDirectory()
        } catch (t: Throwable) {
            Logx.w("No external storage root: ${t.message}")
            return
        }

        val targets = LinkedHashSet<File>()
        val ancestors = LinkedHashSet<File>()

        for (relative in relativePaths) {
            val dir = File(root, relative.trim('/'))
            if (dir.isDirectory) {
                targets.add(dir)
            } else {
                // Watch the nearest EXISTING ancestor for the child appearing.
                var parent: File? = dir.parentFile
                while (parent != null && !parent.isDirectory) parent = parent.parentFile
                if (parent != null && parent.absolutePath.startsWith(root.absolutePath)) {
                    ancestors.add(parent)
                }
            }
        }

        stopObservers()

        if (targets.isNotEmpty()) {
            fileObserver = RecordingFileObserver(
                targets.toList(),
                RecordingFileObserver.MASK_FILES,
            ) { _, path ->
                Logx.d("FileObserver: finished write $path")
                Scheduling.enqueueBurstScan(context)
            }.also { runCatching { it.startWatching() } }
        }

        if (ancestors.isNotEmpty()) {
            ancestorObserver = RecordingFileObserver(
                ancestors.toList(),
                RecordingFileObserver.MASK_DIRS,
            ) { _, path ->
                Logx.d("FileObserver: new entry $path in ancestor; re-arming")
                Scheduling.enqueueBurstScan(context)
                rearm()
            }.also { runCatching { it.startWatching() } }
        }

        watchedDirs = targets.map { it.absolutePath }
        Logx.i("Armed on ${targets.size} folder(s), ${ancestors.size} ancestor(s)")
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

    @Synchronized
    fun stop() {
        stopObservers()
        mediaWatcher.unregister()
        callWatcher.unregister()
        started = false
    }
}
