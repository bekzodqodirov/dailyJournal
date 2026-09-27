package uz.miya.companion.work

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

object Scheduling {

    const val UNIQUE_PERIODIC_SCAN = "miya-periodic-scan"
    const val UNIQUE_BURST_SCAN = "miya-burst-scan"
    const val UNIQUE_DRAIN = "miya-drain-queue"
    const val UNIQUE_EVENT_SYNC = "miya-event-sync"

    /**
     * RECONCILIATION, not detection. 15 minutes is MIN_PERIODIC_INTERVAL_MILLIS;
     * anything shorter is silently clamped. Execution is deferrable and on
     * MIUI/EMUI it may effectively never run — which is exactly why the
     * call-end trigger is primary and this is only a net.
     *
     * No network constraint: scanning and hashing are local work, and the
     * queue must fill even in airplane mode.
     */
    fun ensurePeriodicScan(context: Context) {
        val request = PeriodicWorkRequestBuilder<ScanWorker>(15, TimeUnit.MINUTES)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 5, TimeUnit.MINUTES)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            UNIQUE_PERIODIC_SCAN,
            ExistingPeriodicWorkPolicy.KEEP,
            request,
        )
    }

    /**
     * A short-delayed one-shot used by the file and MediaStore observers.
     *
     * KEEP with an 8 s delay is the debounce: a burst of CLOSE_WRITE events
     * (OEMs rewrite metadata and fire twice) collapses into one scan, and the
     * delay gives a rename-after-write time to land.
     */
    fun enqueueBurstScan(context: Context) {
        // NOT expedited: WorkManager rejects an expedited request that also
        // carries an initial delay, and the delay is the whole point here.
        val request = OneTimeWorkRequestBuilder<ScanWorker>()
            .setInitialDelay(8, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            UNIQUE_BURST_SCAN,
            ExistingWorkPolicy.KEEP,
            request,
        )
    }

    /** Immediate scan on app open / boot / folder change. */
    fun enqueueImmediateScan(context: Context) {
        val request = OneTimeWorkRequestBuilder<ScanWorker>()
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            UNIQUE_BURST_SCAN,
            ExistingWorkPolicy.REPLACE,
            request,
        )
    }

    /**
     * Phone events (build step 6): harvest new call-log rows and SMS, then
     * POST them. Enqueued from every call end and every SMS receipt, and it
     * also rides the 15-minute ScanWorker as a safety sweep.
     *
     * KEEP with a 10 s delay is the same debounce as [enqueueBurstScan] — a
     * burst of SMS collapses into one run — and the delay gives the dialer
     * time to commit the call-log row the trigger fired for. CONNECTED
     * always, never UNMETERED: these are a few KB of JSON, and a missed call
     * should reach the server within a minute of the ring even on mobile
     * data with Wi-Fi-only uploads configured.
     */
    fun enqueueEventSync(context: Context) {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        val request = OneTimeWorkRequestBuilder<EventSyncWorker>()
            .setInitialDelay(10, TimeUnit.SECONDS)
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            UNIQUE_EVENT_SYNC,
            ExistingWorkPolicy.KEEP,
            request,
        )
    }

    /**
     * Re-arm every PENDING row after a reboot or a network return. Cheap: the
     * per-row unique work is KEEP, so anything already scheduled is untouched.
     */
    fun enqueueDrain(context: Context) {
        // CONNECTED, not UNMETERED, even in Wi-Fi-only mode: this worker only
        // re-enqueues per-row upload work, and each of those rows carries the
        // real network constraint from RecordingRepository.scheduleUpload().
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        val request = OneTimeWorkRequestBuilder<DrainWorker>()
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            UNIQUE_DRAIN,
            ExistingWorkPolicy.REPLACE,
            request,
        )
    }
}
