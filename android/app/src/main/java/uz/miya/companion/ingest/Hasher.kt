package uz.miya.companion.ingest

import android.content.Context
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.security.MessageDigest

/**
 * Streaming SHA-256. Never readBytes() a 60 MB recording into a phone's heap,
 * and never hash on the main thread.
 *
 * The digest is over the bytes exactly as they will be uploaded, because the
 * server recomputes it while spooling and 422s on a mismatch (§2.6 step 6).
 */
object Hasher {

    private const val BUFFER = 1 shl 20 // 1 MiB, matching the server's chunk size

    data class Result(val sha256: String, val sizeBytes: Long)

    suspend fun sha256(context: Context, uri: Uri): Result = withContext(Dispatchers.IO) {
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        val stream = context.contentResolver.openInputStream(uri)
            ?: throw IOException("Cannot open $uri")
        stream.use { input ->
            val buffer = ByteArray(BUFFER)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
                total += read
            }
        }
        Result(digest.digest().toHex(), total)
    }

    private fun ByteArray.toHex(): String {
        val chars = "0123456789abcdef"
        val sb = StringBuilder(size * 2)
        for (b in this) {
            val v = b.toInt() and 0xFF
            sb.append(chars[v ushr 4]).append(chars[v and 0x0F])
        }
        return sb.toString()
    }
}
