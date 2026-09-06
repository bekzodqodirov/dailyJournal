package uz.miya.companion.util

import android.util.Log

/**
 * One log tag for the whole app, and one place that knows which values must
 * never reach logcat. The bearer token is the obvious one; phone numbers are
 * the less obvious one.
 */
object Logx {
    const val TAG = "MIYA"

    fun d(msg: String) {
        Log.d(TAG, msg)
    }

    fun i(msg: String) {
        Log.i(TAG, msg)
    }

    fun w(msg: String, t: Throwable? = null) {
        Log.w(TAG, msg, t)
    }

    fun e(msg: String, t: Throwable? = null) {
        Log.e(TAG, msg, t)
    }

    /** Redact all but the last two digits of a phone number. */
    fun redactPhone(number: String?): String {
        if (number.isNullOrBlank()) return "null"
        val digits = number.filter { it.isDigit() }
        if (digits.length <= 2) return "**"
        return "*".repeat(digits.length - 2) + digits.takeLast(2)
    }

    fun shortSha(sha: String?): String = sha?.take(12) ?: "null"
}
