package uz.miya.companion.service

import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import uz.miya.companion.Graph
import uz.miya.companion.util.Logx
import uz.miya.companion.util.Notifications
import uz.miya.companion.work.Scheduling
import java.util.concurrent.atomic.AtomicInteger
import kotlin.coroutines.cancellation.CancellationException

/**
 * A SHORT-LIVED dataSync foreground service, started on call end.
 *
 * It is deliberately not a resident watcher, because on Android 15+ a resident
 * dataSync service is impossible: the type is capped at 6 cumulative hours per
 * 24 h, after which onTimeout() fires and you have seconds to stopSelf() or
 * the user sees an ANR — and that budget only resets when the user brings the
 * app to the foreground. So: scan, hash, enqueue, stop, all within seconds.
 *
 * Everything durable lives in Room and WorkManager, so if this service is
 * killed mid-burst the cost is latency, never data.
 */
class IngestService : Service() {

    private val job = SupervisorJob()
    private val scope = CoroutineScope(job + Dispatchers.IO)
    private val running = AtomicInteger(0)
    private var lastStartId = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Graph.init(applicationContext)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        lastStartId = startId

        // Must happen within 10 seconds of the start request, before anything
        // that can block. Everything after this point is allowed to be slow.
        try {
            ServiceCompat.startForeground(
                this,
                Notifications.ID_SERVICE,
                Notifications.serviceNotification(this, "Checking for a new recording…"),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } catch (t: Throwable) {
            // On Android 12+ this can be refused outright if we were started
            // from the background without an exemption. Fall back to
            // WorkManager rather than dying — but RECORD the reason, because
            // this catch also swallows the foreground-service-type and
            // permission errors that are otherwise undiagnosable, and because
            // a burst that silently never happens looks exactly like an app
            // that is working.
            val reason = "Foreground burst refused: ${t.javaClass.simpleName}" +
                (t.message?.let { ": $it" } ?: "")
            Logx.w(reason)
            Graph.appScope.launch { Graph.prefs.setLastError(reason) }
            Scheduling.enqueueBurstScan(this)
            stopSelf(startId)
            return START_NOT_STICKY
        }

        running.incrementAndGet()
        scope.launch {
            try {
                val result = Graph.scanner.scan()
                Logx.i("Burst scan enqueued ${result.enqueued} of ${result.inspected}")
            } catch (c: CancellationException) {
                // onTimeout()/onDestroy() cancelled us. Not an error, and the
                // suspend setLastError below would throw again in an
                // already-cancelled scope.
                throw c
            } catch (t: Throwable) {
                Logx.e("Burst scan failed", t)
                Graph.prefs.setLastError("Burst scan failed: ${t.message}")
            } finally {
                if (running.decrementAndGet() <= 0) {
                    stopSelf(startId)
                }
            }
        }

        // NOT sticky: a restarted-with-null-intent burst has nothing to do,
        // and the periodic ScanWorker already covers the reconciliation case.
        return START_NOT_STICKY
    }

    /**
     * Android 14: Service.onTimeout(int). Android 15: onTimeout(int, int).
     * Both must stop the service within a few seconds or the user gets
     * "A foreground service of dataSync did not stop within its timeout" —
     * a visible ANR, not a silent stop.
     */
    override fun onTimeout(startId: Int) {
        Logx.w("FGS timeout (startId=$startId); stopping immediately")
        stopImmediately()
    }

    override fun onTimeout(startId: Int, fgsType: Int) {
        Logx.w("FGS timeout (startId=$startId type=$fgsType); stopping immediately")
        stopImmediately()
    }

    private fun stopImmediately() {
        // Hand the remaining work to WorkManager; do not try to finish it here.
        Scheduling.enqueueBurstScan(this)
        job.cancel()
        try {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        } catch (t: Throwable) {
            Logx.w("stopForeground failed", t)
        }
        stopSelf()
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        const val ACTION_SCAN = "uz.miya.companion.action.SCAN"
    }
}
