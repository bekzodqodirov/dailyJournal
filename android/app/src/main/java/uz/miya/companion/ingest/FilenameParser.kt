package uz.miya.companion.ingest

import uz.miya.companion.util.Logx
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * Fallback correlation, used only when READ_CALL_LOG is unavailable or no call
 * row matches.
 *
 * Rules learned the hard way:
 *  - NEVER regex on English literals. "Call recording" is localized; on a
 *    Hindi, Turkish or Arabic device the Samsung prefix is a translated string.
 *    Anchor on digit runs and the extension instead.
 *  - The embedded timestamp is LOCAL WALL CLOCK with no zone, and it is usually
 *    call START, whereas the file's mtime is usually call END. Never mix them.
 *  - The counterparty is a CONTACT NAME when the number is in contacts and a
 *    raw number when it is not, so a number is a bonus, not an expectation.
 */
object FilenameParser {

    data class Parsed(
        val startedAtMillis: Long?,
        val rawNumber: String?,
        val nameHint: String?,
    )

    // yyyy-MM-dd HH-mm-ss  /  yyyy_MM_dd HH:mm:ss  (Vivo-style, space separated)
    private val FULL_DASHED = Regex(
        """(\d{4})[-_.](\d{2})[-_.](\d{2})[ _T-]+(\d{2})[-_.:](\d{2})[-_.:](\d{2})"""
    )

    // yyyyMMdd_HHmmss  /  yyyyMMddHHmmss
    private val COMPACT_FULL = Regex("""(?<!\d)(\d{4})(\d{2})(\d{2})[ _-]?(\d{2})(\d{2})(\d{2})(?!\d)""")

    // yyMMdd_HHmmss   (Samsung One UI)
    private val COMPACT_SHORT = Regex("""(?<!\d)(\d{2})(\d{2})(\d{2})[ _-](\d{2})(\d{2})(\d{2})(?!\d)""")

    /** A run of 7..15 digits, i.e. plausibly a phone number. */
    private val DIGIT_RUN = Regex("""\+?\d[\d\s\-()]{5,17}\d""")

    fun parse(displayName: String, zone: ZoneId = ZoneId.systemDefault()): Parsed {
        val stem = displayName.substringBeforeLast('.', displayName)

        val timestamp = parseTimestamp(stem, zone)

        // Strip whatever the timestamp consumed before looking for a number,
        // so "20250817_143025" is not mistaken for a phone number.
        val withoutTimestamp = stem
            .replace(FULL_DASHED, " ")
            .replace(COMPACT_FULL, " ")
            .replace(COMPACT_SHORT, " ")

        val number = DIGIT_RUN.findAll(withoutTimestamp)
            .map { it.value.trim() }
            .firstOrNull { it.filter(Char::isDigit).length in 7..15 }

        val nameHint = withoutTimestamp
            .replace(DIGIT_RUN, " ")
            .replace('_', ' ')
            .replace(Regex("""[\s\-.]+"""), " ")
            .trim()
            .takeIf { it.length >= 2 }

        return Parsed(timestamp, number, nameHint)
    }

    private fun parseTimestamp(stem: String, zone: ZoneId): Long? {
        FULL_DASHED.find(stem)?.let { m ->
            build(
                m.groupValues[1].toInt(), m.groupValues[2].toInt(), m.groupValues[3].toInt(),
                m.groupValues[4].toInt(), m.groupValues[5].toInt(), m.groupValues[6].toInt(), zone,
            )?.let { return it }
        }
        COMPACT_FULL.find(stem)?.let { m ->
            build(
                m.groupValues[1].toInt(), m.groupValues[2].toInt(), m.groupValues[3].toInt(),
                m.groupValues[4].toInt(), m.groupValues[5].toInt(), m.groupValues[6].toInt(), zone,
            )?.let { return it }
        }
        COMPACT_SHORT.find(stem)?.let { m ->
            build(
                 2000 + m.groupValues[1].toInt(), m.groupValues[2].toInt(), m.groupValues[3].toInt(),
                m.groupValues[4].toInt(), m.groupValues[5].toInt(), m.groupValues[6].toInt(), zone,
            )?.let { return it }
        }
        return null
    }

    private fun build(
        year: Int, month: Int, day: Int, hour: Int, minute: Int, second: Int, zone: ZoneId,
    ): Long? {
        if (year !in 2005..2100) return null
        if (month !in 1..12 || day !in 1..31) return null
        if (hour !in 0..23 || minute !in 0..59 || second !in 0..59) return null
        return try {
            LocalDateTime.of(year, month, day, hour, minute, second)
                .atZone(zone)
                .toInstant()
                .toEpochMilli()
        } catch (t: Throwable) {
            Logx.w("Bad filename timestamp $year-$month-$day $hour:$minute:$second")
            null
        }
    }
}
