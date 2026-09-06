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

    /**
     * A call-recording FILENAME is personal data, not a technical detail: on
     * Samsung and Xiaomi it contains the counterparty's contact name and very
     * often their phone number ("Call recording Akmal aka_250817_143025.m4a",
     * "+998901234567_20250817.m4a"). Logcat is readable over adb and by crash
     * and diagnostic tooling, so nothing in this app logs one verbatim.
     *
     * What survives is enough to correlate two log lines about the same file —
     * a stable short hash of the stem — plus the extension, which is what the
     * ingest gates are actually about.
     */
    fun redactName(name: String?): String {
        if (name.isNullOrBlank()) return "null"
        // A FileObserver path may carry directories; only the last segment can
        // contain the counterparty, but the parents can leak the folder layout,
        // so keep just the leaf.
        val leaf = name.substringAfterLast('/')
        val dot = leaf.lastIndexOf('.')
        val stem = if (dot > 0) leaf.substring(0, dot) else leaf
        val ext = if (dot > 0) leaf.substring(dot).lowercase() else ""
        val digest = Integer.toHexString(stem.hashCode()).takeLast(8).padStart(8, '0')
        return "«$digest»$ext"
    }

    fun shortSha(sha: String?): String = sha?.take(12) ?: "null"
}
