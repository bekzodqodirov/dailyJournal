package uz.miya.companion.util

import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * The server contract wants ISO-8601 *with a real UTC offset* (§2.5). Anything
 * that drops the offset re-introduces the exact bug the metadata was added to
 * kill, so timestamps are formatted in exactly one place.
 *
 * java.time is available natively from API 26; minSdk here is 29, so no
 * desugaring is required.
 */
object TimeFmt {

    fun isoOffset(epochMillis: Long, zone: ZoneId = ZoneId.systemDefault()): String =
        OffsetDateTime.ofInstant(Instant.ofEpochMilli(epochMillis), zone)
            .withNano(0)
            .format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)

    fun nowIso(): String = isoOffset(System.currentTimeMillis())

    /** Human-readable, local, for the UI only. Never sent to the server. */
    fun human(epochMillis: Long?): String {
        if (epochMillis == null || epochMillis <= 0L) return "never"
        return OffsetDateTime.ofInstant(Instant.ofEpochMilli(epochMillis), ZoneId.systemDefault())
            .format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm"))
    }

    fun ago(epochMillis: Long?): String {
        if (epochMillis == null || epochMillis <= 0L) return "never"
        val delta = System.currentTimeMillis() - epochMillis
        if (delta < 0) return human(epochMillis)
        val minutes = delta / 60_000
        return when {
            minutes < 1 -> "just now"
            minutes < 60 -> "$minutes min ago"
            minutes < 60 * 24 -> "${minutes / 60} h ago"
            else -> "${minutes / (60 * 24)} d ago"
        }
    }
}
