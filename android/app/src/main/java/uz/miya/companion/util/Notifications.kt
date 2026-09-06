package uz.miya.companion.util

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import uz.miya.companion.R
import uz.miya.companion.ui.MainActivity

object Notifications {

    const val CHANNEL_SERVICE = "miya_service"
    const val CHANNEL_ALERTS = "miya_alerts"

    /** Foreground-service notification id. Stable so bursts reuse one slot. */
    const val ID_SERVICE = 1001
    const val ID_STALLED = 1002
    const val ID_PRECONDITION = 1003

    fun createChannels(context: Context) {
        val mgr = context.getSystemService(NotificationManager::class.java) ?: return

        val service = NotificationChannel(
            CHANNEL_SERVICE,
            context.getString(R.string.channel_service_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = context.getString(R.string.channel_service_desc)
            setShowBadge(false)
        }

        val alerts = NotificationChannel(
            CHANNEL_ALERTS,
            context.getString(R.string.channel_alerts_name),
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = context.getString(R.string.channel_alerts_desc)
        }

        mgr.createNotificationChannel(service)
        mgr.createNotificationChannel(alerts)
    }

    private fun openApp(context: Context): PendingIntent =
        PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    /**
     * The ongoing notification for the dataSync foreground service. Low
     * importance, silent, but *present* — a foreground service whose
     * notification is suppressed (POST_NOTIFICATIONS denied) is far more
     * likely to be killed, and invisible when it is.
     */
    fun serviceNotification(context: Context, text: String): Notification =
        NotificationCompat.Builder(context, CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_stat_miya)
            .setContentTitle("MIYA")
            .setContentText(text)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(openApp(context))
            .build()

    fun alert(context: Context, id: Int, title: String, text: String) {
        val n = NotificationCompat.Builder(context, CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_miya)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(openApp(context))
            .build()
        try {
            NotificationManagerCompat.from(context).notify(id, n)
        } catch (se: SecurityException) {
            // POST_NOTIFICATIONS not granted. Health screen still shows it.
            Logx.w("Cannot post notification: ${se.message}")
        }
    }

    fun cancel(context: Context, id: Int) {
        try {
            NotificationManagerCompat.from(context).cancel(id)
        } catch (t: Throwable) {
            Logx.w("cancel notification failed", t)
        }
    }
}
