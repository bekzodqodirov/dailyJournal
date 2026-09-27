package uz.miya.companion.ingest

import uz.miya.companion.util.Keys
import uz.miya.companion.util.TimeFmt
import java.time.ZoneId

/**
 * One posted notification, as the listener read it. Plain values only, so
 * every decision below is a pure function a JVM test can call.
 */
data class NotificationFields(
    val title: String?,
    val text: String?,
    val bigText: String?,
    val subText: String?,
    val lines: List<String>,
    val whenMs: Long,
    val postTimeMs: Long,
    val id: Int,
    val tag: String?,
    val channelId: String?,
    val category: String?,
    val isGroupSummary: Boolean,
    val isOngoing: Boolean,
)

/**
 * Payment-app notifications (WP-64): what is captured, how it is clipped,
 * and the dedupe key — mirrored exactly by the server's
 * notification_event_key (miya/services/phone_events.py).
 */
object PaymentApps {
    /** Notification.CATEGORY_PROGRESS, spelled out to stay android-free. */
    const val CATEGORY_PROGRESS = "progress"

    // The server's field limits (NotificationIn); clipping to them here
    // means the key is minted over exactly what the server will see.
    private const val MAX_TEXT = 4096
    private const val MAX_SHORT = 512
    private const val MAX_ID = 255
    private const val MAX_CATEGORY = 64
    private const val MAX_LINES = 20
    private const val MAX_LINE = 512

    /**
     * Only an allow-listed package is read; our own app and the SMS app
     * (the SMS path covers SMS) never; nor summaries, ongoing or progress
     * notifications, nor ones with no text at all.
     */
    fun shouldCapture(
        pkg: String,
        f: NotificationFields,
        allow: Set<String>,
        ownPkg: String,
        smsPkg: String?,
    ): Boolean {
        if (pkg == ownPkg) return false
        if (smsPkg != null && pkg == smsPkg) return false
        if (pkg !in allow) return false
        if (f.isGroupSummary || f.isOngoing || f.category == CATEGORY_PROGRESS) return false
        val texts = listOf(f.title, f.text, f.bigText, f.subText) + f.lines
        return texts.any { !it.isNullOrBlank() }
    }

    fun clipped(f: NotificationFields): NotificationFields = f.copy(
        title = f.title?.let { Keys.clipUtf16(it, MAX_SHORT) },
        text = f.text?.let { Keys.clipUtf16(it, MAX_TEXT) },
        bigText = f.bigText?.let { Keys.clipUtf16(it, MAX_TEXT) },
        subText = f.subText?.let { Keys.clipUtf16(it, MAX_SHORT) },
        lines = f.lines.take(MAX_LINES).map { Keys.clipUtf16(it, MAX_LINE) },
        tag = f.tag?.let { Keys.clipUtf16(it, MAX_ID) },
        channelId = f.channelId?.let { Keys.clipUtf16(it, MAX_ID) },
        category = f.category?.let { Keys.clipUtf16(it, MAX_CATEGORY) },
    )

    /** big_text, else text ('' counts as missing), then the inbox lines. */
    fun body(f: NotificationFields): String {
        val main = (f.bigText?.takeIf { it.isNotEmpty() } ?: f.text?.takeIf { it.isNotEmpty() })
            .orEmpty()
        return main + if (f.lines.isEmpty()) "" else "\n" + f.lines.joinToString("\n")
    }

    fun notificationKey(deviceId: String, pkg: String, f: NotificationFields): String {
        // The server only sees whole seconds (when_at is sent truncated).
        val whenSec = Math.floorDiv(f.whenMs, 1000L) * 1000L
        val material = "${f.id}|${f.tag.orEmpty()}|$whenSec|${f.title.orEmpty()}|${body(f)}"
        return "$deviceId:ntf:$pkg:" + Keys.sha256hex16(material)
    }

    fun payloadMap(
        pkg: String,
        f: NotificationFields,
        zone: ZoneId = ZoneId.systemDefault(),
    ): Map<String, Any?> = linkedMapOf(
        "package" to pkg,
        "posted_at" to TimeFmt.isoOffsetExact(f.postTimeMs, zone),
        "when_at" to TimeFmt.isoOffsetExact(f.whenMs, zone),
        "title" to f.title,
        "text" to f.text,
        "big_text" to f.bigText,
        "sub_text" to f.subText,
        "lines" to f.lines,
        "notification_id" to f.id,
        "tag" to f.tag,
        "channel_id" to f.channelId,
        "category" to f.category,
    )

    /**
     * Payme is pre-ticked by its label the first time it notifies — no
     * package id is hard-coded — unless the owner has unticked it before.
     */
    fun shouldAutoTick(
        label: String?,
        pkg: String,
        allow: Set<String>,
        unticked: Set<String>,
    ): Boolean =
        label?.trim()?.equals("Payme", ignoreCase = true) == true &&
            pkg !in allow && pkg !in unticked
}
