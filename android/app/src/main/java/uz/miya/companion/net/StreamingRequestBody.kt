package uz.miya.companion.net

import android.content.ContentResolver
import android.net.Uri
import okhttp3.MediaType
import okhttp3.RequestBody
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

    override fun writeTo(sink: BufferedSink) {
        val input = resolver.openInputStream(uri)
            ?: throw IOException("Source recording is no longer readable: $uri")
        input.use { sink.writeAll(it.source()) }
    }
}
