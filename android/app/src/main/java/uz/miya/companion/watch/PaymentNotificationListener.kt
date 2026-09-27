package uz.miya.companion.watch

import android.app.Notification
import android.content.ComponentName
import android.content.pm.PackageManager
import android.provider.Telephony
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import uz.miya.companion.Graph
import uz.miya.companion.data.PhoneEventEntity
import uz.miya.companion.data.PhoneEventKind
import uz.miya.companion.ingest.NotificationFields
import uz.miya.companion.ingest.PaymentApps
import uz.miya.companion.util.Logx
import uz.miya.companion.work.Scheduling

/**
 * Payment-app pushes (WP-64). Every notification is seen by package NAME
 * only, so the chooser in Settings can list it; the text of a notification
 * is read only when its package is on the owner's allow-list, and then it
 * is queued in Room exactly like an SMS and posted to the owner's server.
 *
 * Notification text is money data and is NEVER logged.
 */
class PaymentNotificationListener : NotificationListenerService() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onListenerConnected() {
        super.onListenerConnected()
        Graph.init(applicationContext)
        instance = this
        scope.launch {
            Graph.prefs.setListenerConnectedAt(System.currentTimeMillis())
            captureActive(null)
        }
    }

    override fun onListenerDisconnected() {
        instance = null
        scope.launch { Graph.prefs.setListenerConnectedAt(null) }
        try {
            requestRebind(ComponentName(this, PaymentNotificationListener::class.java))
        } catch (t: Throwable) {
            Logx.w("Listener rebind failed: ${t.message}")
        }
        super.onListenerDisconnected()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        scope.launch { capture(sbn) }
    }

    override fun onDestroy() {
        instance = null
        scope.cancel()
        super.onDestroy()
    }

    private suspend fun captureActive(pkg: String?) {
        val active = try {
            activeNotifications ?: emptyArray()
        } catch (t: Throwable) {
            Logx.w("Could not read active notifications: ${t.message}")
            emptyArray()
        }
        for (sbn in active) {
            if (pkg == null || sbn.packageName == pkg) capture(sbn)
        }
    }

    private suspend fun capture(sbn: StatusBarNotification) {
        try {
            val prefs = Graph.prefs
            val pkg = sbn.packageName ?: return
            if (pkg == packageName) return
            prefs.noteSeenPackage(pkg)

            var snapshot = prefs.snapshot()
            if (PaymentApps.shouldAutoTick(
                    labelOf(pkg),
                    pkg,
                    snapshot.paymentAppPackages,
                    snapshot.untickedPackages,
                )
            ) {
                prefs.autoTickPayment(pkg)
                snapshot = prefs.snapshot()
            }

            val fields = fieldsOf(sbn)
            val smsPkg = try {
                Telephony.Sms.getDefaultSmsPackage(this)
            } catch (t: Throwable) {
                null
            }
            if (!PaymentApps.shouldCapture(
                    pkg,
                    fields,
                    snapshot.paymentAppPackages,
                    packageName,
                    smsPkg,
                )
            ) {
                return
            }
            val clipped = PaymentApps.clipped(fields)
            val payload = JSONObject()
            for ((key, value) in PaymentApps.payloadMap(pkg, clipped)) {
                when (value) {
                    null -> payload.put(key, JSONObject.NULL)
                    is List<*> -> payload.put(key, JSONArray(value))
                    else -> payload.put(key, value)
                }
            }
            val key = PaymentApps.notificationKey(prefs.deviceId(), pkg, clipped)
            Graph.database.phoneEvents().insertIgnore(
                listOf(
                    PhoneEventEntity(
                        key = key,
                        kind = PhoneEventKind.NOTIFICATION,
                        payloadJson = payload.toString(),
                    )
                )
            )
            prefs.markPaymentNotification()
            Scheduling.enqueueEventSync(applicationContext)
            // The package only — never the title, the text or the lines.
            Logx.i("Queued a payment notification from $pkg")
        } catch (t: Throwable) {
            Logx.w("Payment notification capture failed: ${t.javaClass.simpleName}")
        }
    }

    private fun labelOf(pkg: String): String? = try {
        val info = packageManager.getApplicationInfo(pkg, 0)
        packageManager.getApplicationLabel(info).toString()
    } catch (e: PackageManager.NameNotFoundException) {
        null
    } catch (t: Throwable) {
        null
    }

    private fun fieldsOf(sbn: StatusBarNotification): NotificationFields {
        val n = sbn.notification
        val extras = n.extras
        val whenMs = if (n.`when` > 0) n.`when` else sbn.postTime
        return NotificationFields(
            title = extras?.getCharSequence(Notification.EXTRA_TITLE)?.toString(),
            text = extras?.getCharSequence(Notification.EXTRA_TEXT)?.toString(),
            bigText = extras?.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString(),
            subText = extras?.getCharSequence(Notification.EXTRA_SUB_TEXT)?.toString(),
            lines = extras?.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
                ?.mapNotNull { it?.toString() }
                .orEmpty(),
            whenMs = whenMs,
            postTimeMs = sbn.postTime,
            id = sbn.id,
            tag = sbn.tag,
            channelId = n.channelId,
            category = n.category,
            isGroupSummary = (n.flags and Notification.FLAG_GROUP_SUMMARY) != 0,
            isOngoing = sbn.isOngoing,
        )
    }

    companion object {
        @Volatile
        private var instance: PaymentNotificationListener? = null

        /**
         * Right after the owner ticks [pkg]: capture what is still in the
         * shade, so the notification that made the package appear in the
         * list is not lost. A no-op while the listener is not connected.
         */
        fun captureActiveFor(pkg: String) {
            val listener = instance ?: return
            listener.scope.launch { listener.captureActive(pkg) }
        }
    }
}
