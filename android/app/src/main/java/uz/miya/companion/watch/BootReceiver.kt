package uz.miya.companion.watch

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import uz.miya.companion.Graph
import uz.miya.companion.util.Logx
import uz.miya.companion.work.Scheduling

/**
 * After a reboot or an app update, re-register the observers and reconcile
 * from disk.
 *
 * It deliberately does NOT start the foreground service: an app targeting
 * Android 15+ may not launch a dataSync foreground service from a
 * BOOT_COMPLETED receiver at all. WorkManager is the correct path here, and
 * the queue is durable, so the cost of the delay is latency and nothing else.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        Logx.i("BootReceiver: $action")
        when (action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            -> {
                Graph.init(context.applicationContext)
                Graph.watchArmer.start()
                Scheduling.ensurePeriodicScan(context)
                Scheduling.enqueueImmediateScan(context)
                Scheduling.enqueueDrain(context)
            }
        }
    }
}
