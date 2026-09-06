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
 *   2. Size. Must be non-zero and unchanged across ~2 s of polling.
 *   3. Container. MediaMetadataRetriever must return a positive duration —
 *      a truncated MP4 has no readable moov atom and fails here.
 */
object StabilityGate {

    data class Verdict(val ready: Boolean, val durationMillis: Long?, val reason: String?)

    private const val SAMPLES = 3
    private const val INTERVAL_MILLIS = 1000L

    suspend fun check(context: Context, ref: RecordingRef): Verdict {
        if (!FolderProbe.isAudioName(ref.displayName)) {
            return Verdict(false, null, "not an audio filename")
        }

        var previous = currentSize(context, ref.uri)
        if (previous <= 0L) return Verdict(false, null, "zero-length or unreadable")

        repeat(SAMPLES - 1) {
            delay(INTERVAL_MILLIS)
            val now = currentSize(context, ref.uri)
            if (now != previous) {
                return Verdict(false, null, "still being written ($previous -> $now)")
            }
            previous = now
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
        Logx.w("Cannot stat $uri: ${t.message}")
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
