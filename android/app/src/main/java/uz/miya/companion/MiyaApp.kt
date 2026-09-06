package uz.miya.companion

import android.app.Application
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import uz.miya.companion.util.Logx
import uz.miya.companion.util.Notifications
import uz.miya.companion.watch.WatchArmer
import uz.miya.companion.work.Scheduling

/**
 * Holds the process-lifetime references — most importantly the [WatchArmer],
 * and through it the FileObserver instances. A garbage-collected FileObserver
 * stops delivering events with no error of any kind: it works in a debug build
 * and dies in release, which is the worst possible failure shape.
 */
class MiyaApp : Application() {

    override fun onCreate() {
        super.onCreate()
        Graph.init(this)
        Notifications.createChannels(this)

        // Materialise the device id once, at first launch: it is half of the
        // call_id the server de-duplicates on, and a REQUIRED contract field.
        Graph.appScope.launch { Graph.prefs.deviceId() }

        // Pre-warm the token store OFF the main thread. The first touch loads a
        // SharedPreferences file from disk and, if a token is present, runs an
        // AndroidKeyStore lookup — work the Health screen would otherwise do on
        // Dispatchers.Main during onboarding.
        Graph.appScope.launch(Dispatchers.IO) { Graph.tokenStore.hasToken() }

        // Reconciliation sweep. KEEP, so an existing schedule is not reset on
        // every process start (which would mean it never actually runs).
        Scheduling.ensurePeriodicScan(this)

        // Observers. Cheap, and safe to call when permissions are missing —
        // each sub-watcher reports failure rather than throwing.
        Graph.watchArmer.start()

        // Re-arm upload work for anything still pending. Costs nothing when
        // the queue is empty, and it is the cheapest insurance against a row
        // whose worker was lost to a force-stop.
        Scheduling.enqueueDrain(this)

        Logx.i("MIYA companion ${BuildConfig.VERSION_NAME} started")
    }
}
