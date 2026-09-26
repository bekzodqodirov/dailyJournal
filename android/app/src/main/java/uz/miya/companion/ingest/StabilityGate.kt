package uz.miya.companion.ingest

import android.content.Context
import android.media.MediaMetadataRetriever
import android.net.Uri
import kotlinx.coroutines.delay
import uz.miya.companion.data.RecordingRef
import uz.miya.companion.discover.FolderProbe
import uz.miya.companion.util.Logx

/**
 * Belt and braces over CLOSE_WRITE.
 *
 * The single worst outcome available to this app is hashing a half-written
 * file: it poisons dedupe with a hash the finished file will never match, so
 * the real recording gets ingested a second time later — and you pay to
 * transcribe truncated audio. Hence three independent gates:
 *
 *   1. Name. Reject .3ga (Samsung writes one DURING the call), .tmp, .part,
 *      dotfiles, and anything that is not a known audio extension.
 *   2. Size. Must be non-zero, and — for a file written within the last few
 *      minutes, which is the only kind a dialer could still have open —
 *      unchanged across ~2 s of polling.
 *   3. Container. MediaMetadataRetriever must return a positive duration —
 *      a truncated MP4 has no readable moov atom and fails here.
 */
object StabilityGate {

    data class Verdict(val ready: Boolean, val durationMillis: Long?, val reason: String?)

    private const val SAMPLES = 3
    private const val INTERVAL_MILLIS = 1000L

    /**
     * How recently a file must have been written for the polling gate to be
     * worth its two seconds. A recording whose mtime is older than this is not
     * being written any more by any dialer on any phone; the size and container
     * gates still apply to it, they just do not need a stopwatch.
     *
     * Without this, a first run on a phone with a year of recordings spent two
     * seconds PER FILE before hashing anything, which on its own can exceed
     * WorkManager's 10-minute execution ceiling.
     */
    private const val FRESH_WINDOW_MILLIS = 5L * 60 * 1000

    suspend fun check(context: Context, ref: RecordingRef): Verdict {
        if (!FolderProbe.isAudioName(ref.displayName)) {
            return Verdict(false, null, "not an audio filename")
        }

        var previous = currentSize(context, ref.uri)
        if (previous <= 0L) return Verdict(false, null, "zero-length or unreadable")

        val fresh = System.currentTimeMillis() - ref.lastModifiedMillis < FRESH_WINDOW_MILLIS
        if (fresh) {
            repeat(SAMPLES - 1) {
                delay(INTERVAL_MILLIS)
                val now = currentSize(context, ref.uri)
                if (now != previous) {
                    return Verdict(false, null, "still being written ($previous -> $now)")
                }
                previous = now
            }
        }

        val duration = durationMillis(context, ref.uri)
        if (duration == null || duration <= 0L) {
            return Verdict(false, null, "container not parseable yet")
        }
        return Verdict(true, duration, null)
    }

    /**
     * Re-read the size from the source rather than trusting the RecordingRef,
     * which may be a MediaStore row cached before the write finished.
     */
    fun currentSize(context: Context, uri: Uri): Long = try {
        context.contentResolver.openFileDescriptor(uri, "r")?.use { pfd ->
            pfd.statSize
        } ?: -1L
    } catch (t: Throwable) {
        // Not the raw URI: a file:// URI for a shared import ends in the
        // dialer's own filename, which carries the counterparty's name.
        Logx.w("Cannot stat ${Logx.redactName(uri.lastPathSegment)}: ${t.message}")
        -1L
    }

    fun durationMillis(context: Context, uri: Uri): Long? {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(context, uri)
            retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
                ?.toLongOrNull()
        } catch (t: Throwable) {
            null
        } finally {
            try {
                retriever.release()
            } catch (t: Throwable) {
                // MediaMetadataRetriever.release() throws IOException on some OEMs.
            }
        }
    }
}
