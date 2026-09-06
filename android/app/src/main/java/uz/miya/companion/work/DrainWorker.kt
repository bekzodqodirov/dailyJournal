package uz.miya.companion.work

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import uz.miya.companion.Graph
import uz.miya.companion.util.Logx

/**
 * Re-arms per-row upload work for everything still pending. Needed after a
 * reboot or a force-stop, where WorkManager keeps its own database but the
 * cheapest way to be certain nothing was orphaned is to ask the queue.
 *
 * Uploads oldest first, which is what the owner expects when a week offline
 * suddenly drains.
 */
class DrainWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        Graph.init(applicationContext)
        return try {
            val pending = Graph.repository.pendingRows(500)
            for (row in pending) Graph.repository.scheduleUpload(row.sha256)
            Logx.i("Drain re-armed ${pending.size} pending upload(s)")
            Result.success()
        } catch (t: Throwable) {
            Logx.e("Drain failed", t)
            Result.retry()
        }
    }
}
