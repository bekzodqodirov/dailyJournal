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

        Notifications.alert(
            applicationContext,
            Notifications.ID_STALLED,
            "MIYA has stopped uploading",
            "Nothing has been accepted since ${TimeFmt.human(last)}. " +
                "Open MIYA and check the Health screen.",
        )
    }

    private companion object {
        const val DAY_MILLIS = 24L * 60 * 60 * 1000
    }
}
