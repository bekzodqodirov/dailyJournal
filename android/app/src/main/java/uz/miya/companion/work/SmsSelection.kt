package uz.miya.companion.work

/**
 * The first SMS import window (WP-65). Pure — column names are literals, so
 * a JVM test never touches android.provider.
 */
object SmsSelection {
    const val DAY_MS = 86_400_000L

    /** The owner's choices; 0 = "Yuklamaslik" (start from now). */
    val BACKFILL_CHOICES = listOf(0, 7, 30, 90, 365)
    const val DEFAULT_BACKFILL_DAYS = 30

    /**
     * The provider selection. While nothing has been harvested yet
     * (afterId == 0) and the cutoff is frozen, only messages dated at or
     * after it are read; afterwards the _id high-water mark alone decides.
     */
    fun smsSelection(afterId: Long, importFromMs: Long?): Pair<String, Array<String>> =
        if (afterId == 0L && importFromMs != null) {
            "_id > ? AND date >= ?" to arrayOf(afterId.toString(), importFromMs.toString())
        } else {
            "_id > ?" to arrayOf(afterId.toString())
        }

    /** The cutoff frozen at the first harvest; never recomputed. */
    fun importFrom(nowMs: Long, days: Int): Long = nowMs - days.coerceAtLeast(0) * DAY_MS
}
