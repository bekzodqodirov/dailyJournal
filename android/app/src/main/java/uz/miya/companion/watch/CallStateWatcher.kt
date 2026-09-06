package uz.miya.companion.watch

import android.Manifest
import android.content.Context
import android.os.Build
import android.telephony.PhoneStateListener
import android.telephony.TelephonyCallback
import android.telephony.TelephonyManager
import androidx.annotation.RequiresApi
import uz.miya.companion.util.Logx
import uz.miya.companion.util.StorageAccess
import java.util.concurrent.Executors

/**
 * The primary trigger. Everything else in this app is a safety net.
 *
 * A call going from a non-idle state to CALL_STATE_IDLE means the OEM dialer
 * has just finished (or is about to finish) writing a file. That is the
 * cheapest reliable signal available without recording anything, and it is the
 * one that beats MIUI's cleaner to the file.
 */
class CallStateWatcher(
    private val context: Context,
    private val onCallEnded: () -> Unit,
) {

    private val executor = Executors.newSingleThreadExecutor()
    private var telephonyCallback: Any? = null
    private var legacyListener: PhoneStateListener? = null
    private var sawNonIdle = false

    fun register(): Boolean {
        if (!StorageAccess.granted(context, Manifest.permission.READ_PHONE_STATE)) {
            Logx.w("READ_PHONE_STATE not granted; call-end trigger disabled")
            return false
        }
        val tm = context.getSystemService(TelephonyManager::class.java) ?: return false
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                registerModern(tm)
            } else {
                registerLegacy(tm)
            }
            true
        } catch (t: Throwable) {
            Logx.e("Cannot register call-state listener", t)
            false
        }
    }

    @RequiresApi(Build.VERSION_CODES.S)
    private fun registerModern(tm: TelephonyManager) {
        val callback = object : TelephonyCallback(), TelephonyCallback.CallStateListener {
            override fun onCallStateChanged(state: Int) = handle(state)
        }
        telephonyCallback = callback
        tm.registerTelephonyCallback(executor, callback)
    }

    @Suppress("DEPRECATION")
    private fun registerLegacy(tm: TelephonyManager) {
        val listener = object : PhoneStateListener() {
            override fun onCallStateChanged(state: Int, phoneNumber: String?) = handle(state)
        }
        legacyListener = listener
        tm.listen(listener, PhoneStateListener.LISTEN_CALL_STATE)
    }

    private fun handle(state: Int) {
        when (state) {
            TelephonyManager.CALL_STATE_OFFHOOK, TelephonyManager.CALL_STATE_RINGING -> {
                sawNonIdle = true
            }
            TelephonyManager.CALL_STATE_IDLE -> {
                if (sawNonIdle) {
                    sawNonIdle = false
                    Logx.i("Call ended; firing ingest burst")
                    onCallEnded()
                }
            }
        }
    }

    @Suppress("DEPRECATION")
    fun unregister() {
        try {
            val tm = context.getSystemService(TelephonyManager::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                (telephonyCallback as? TelephonyCallback)?.let { tm?.unregisterTelephonyCallback(it) }
            } else {
                legacyListener?.let { tm?.listen(it, PhoneStateListener.LISTEN_NONE) }
            }
        } catch (t: Throwable) {
            Logx.w("Cannot unregister call-state listener", t)
        }
        telephonyCallback = null
        legacyListener = null
    }
}
