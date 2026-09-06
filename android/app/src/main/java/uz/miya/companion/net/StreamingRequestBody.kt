package uz.miya.companion.net

import android.content.ContentResolver
import android.net.Uri
import okhttp3.MediaType
import okhttp3.RequestBody
import okio.Buffer
import okio.BufferedSink
import okio.source
import java.io.IOException

/**
 * Streams the recording straight from the ContentResolver into the socket.
 * Never readBytes(): a 60 MB m4a materialised on a phone heap is an OOM on the
 * cheap devices this is most likely to run on.
 *
 * Not one-shot: the stream is reopened per attempt, so OkHttp's own
 * retryOnConnectionFailure and WorkManager's retries both work.
 */
class StreamingRequestBody(
    private val resolver: ContentResolver,
    private val uri: Uri,
    private val mediaType: MediaType?,
    private val declaredLength: Long,
) : RequestBody() {

    override fun contentType(): MediaType? = mediaType

    override fun contentLength(): Long = if (declaredLength > 0) declaredLength else -1L

    /**
     * The bytes are counted as they go out, and a mismatch against the length
     * declared in [contentLength] is raised as a [SourceChangedException] —
     * deliberately NOT an IOException.
     *
     * Why it matters: [declaredLength] is the size recorded when the file was
     * hashed. If the OEM rewrote or truncated the recording afterwards, OkHttp
     * itself would fail with ProtocolException("unexpected end of stream"),
     * which IS an IOException and therefore classified as a transient network
     * error — a row retrying every five hours forever for a reason no amount
     * of network will ever fix. Raising a distinct type lets UploadApi park it
     * and let the next scan re-hash the changed file.
     */
    override fun writeTo(sink: BufferedSink) {
        val input = resolver.openInputStream(uri)
            ?: throw IOException("Source recording is no longer readable: $uri")

        val written = input.use { stream ->
            val source = stream.source()
            val buffer = Buffer()
            var total = 0L
            while (true) {
                val read = source.read(buffer, SEGMENT_BYTES)
                if (read == -1L) break
                sink.write(buffer, read)
                total += read
            }
            total
        }

        if (declaredLength > 0 && written != declaredLength) {
            throw SourceChangedException(
                "expected $declaredLength bytes, read $written"
            )
        }
    }

    /**
     * The source file is not the file we hashed any more. A RuntimeException on
     * purpose: an IOException here would be indistinguishable from a flaky
     * socket and would be retried forever.
     */
    class SourceChangedException(message: String) : RuntimeException(message)

    private companion object {
        const val SEGMENT_BYTES = 64L * 1024
    }
}
