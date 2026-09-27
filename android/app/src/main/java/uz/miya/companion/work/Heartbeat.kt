package uz.miya.companion.work

/**
 * The hourly liveness post (WP-66). Pure — no android.* types — so a JVM
 * test can pin its shape; the server keeps only the known stream keys.
 */
object Heartbeat {
    val STREAMS = listOf("call_log", "sms", "recordings", "notifications")
    const val EVERY_MS = 60L * 60 * 1000

    fun heartbeatMap(
        deviceId: String,
        wanted: Map<String, Boolean>,
        granted: Map<String, Boolean>,
        everGranted: Set<String>,
        queue: Map<String, Int>,
        versionName: String,
        versionCode: Int,
    ): Map<String, Any?> = linkedMapOf(
        "device_id" to deviceId,
        "app_version" to versionName,
        "version_code" to versionCode,
        "wanted" to STREAMS.associateWith { wanted[it] == true },
        "granted" to STREAMS.associateWith { granted[it] == true },
        "ever_granted" to STREAMS.associateWith { it in everGranted || granted[it] == true },
        "queue" to queue,
    )

    fun due(lastAt: Long?, nowMs: Long): Boolean = lastAt == null || nowMs - lastAt >= EVERY_MS
}
