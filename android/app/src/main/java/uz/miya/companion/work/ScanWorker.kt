package uz.miya.companion.work

import android.content.Context
import android.content.pm.ServiceInfo
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import uz.miya.companion.Graph
import uz.miya.companion.util.Logx
import uz.miya.companion.util.Notifications
import uz.miya.companion.util.TimeFmt
import kotlin.coroutines.cancellation.CancellationException

/**
 * The reconciliation sweep. It catches whatever the observers missed — and on
 * an aggressive MIUI build with the toggles reset, it may be the only thing
 * that ever runs, so it also owns the "we have gone quiet" alarm.
 */
class ScanWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    override suspend fun getForegroundInfo(): ForegroundInfo =
        ForegroundInfo(
            Notifications.ID_SERVICE,
            Notifications.serviceNotification(applicationContext, "Checking for new recordings…"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
        )

    override suspend fun doWork(): Result {
        Graph.init(applicationContext)
        return try {
            val result = Graph.scanner.scan()
            Logx.i(
                "Scan: inspected=${result.inspected} enqueued=${result.enqueued} " +
                    "unstable=${result.skippedUnstable} tooShort=${result.skippedTooShort}"
            )
            checkForSilence()
            Result.success()
        } catch (c: CancellationException) {
            // WorkManager stopping the worker (10-minute ceiling, constraint
            // lost, cancelled by REPLACE) cancels this coroutine. Swallowing it
            // and then calling a suspend Prefs setter in an already-cancelled
            // scope throws a SECOND CancellationException that escapes as a
            // hard failure. Cancellation is not an error: rethrow it and let
            // WorkManager reschedule.
            throw c
        } catch (t: Throwable) {
            Logx.e("Scan failed", t)
            Graph.prefs.setLastError("Scan failed: ${t.message}")
            Result.retry()
        }
    }

    /**
     * §4.2: silence is the enemy. If there is something in the queue and
     * nothing has been accepted for 24 h, say so out loud — a stuck queue that
     * nobody can see is worse than a crash.
     */
    private suspend fun checkForSilence() {
        val dao = Graph.database.uploads()
        val pending = dao.nextPending(1)
        if (pending.isEmpty()) {
            Notifications.cancel(applicationContext, Notifications.ID_STALLED)
            return
        }
        val last = dao.lastUploadAt() ?: 0L
        val quietFor = System.currentTimeMillis() - last
        if (last > 0L && quietFor < DAY_MILLIS) return
        if (last == 0L && System.currentTimeMillis() - pending.first().createdAt < DAY_MILLIS) return

        // "Nothing has been accepted since never" is what TimeFmt.human(0)
        // produced, in the single most important failure case there is — the
        // app has never worked at all — and it read as a bug in the alert
        // rather than as a report about the queue.
        val oldest = pending.first().createdAt
        if (last == 0L) {
            Notifications.alert(
                applicationContext,
                Notifications.ID_STALLED,
                "MIYA has never uploaded anything",
                "The oldest recording has been waiting since ${TimeFmt.human(oldest)}. " +
                    "Open MIYA — the Health screen names the failing step.",
            )
        } else {
            Notifications.alert(
                applicationContext,
                Notifications.ID_STALLED,
                "MIYA has stopped uploading",
                "Nothing has been accepted since ${TimeFmt.human(last)}. " +
                    "Open MIYA and check the Health screen.",
            )
        }
    }

    private companion object {
        const val DAY_MILLIS = 24L * 60 * 60 * 1000
    }
}
