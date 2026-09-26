package uz.miya.companion.discover

import android.content.ContentUris
import android.content.Context
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import uz.miya.companion.data.RecordingRef
import uz.miya.companion.util.Logx

/**
 * MediaStore is the fast path: on Android 11+ MediaProvider runs over FUSE, so
 * an app holding READ_MEDIA_AUDIO can read other apps' audio both through
 * content URIs and through raw paths.
 *
 * It is a fast path and not the only one, because it fails outright when the
 * OEM (or a how-to guide the owner followed) drops a .nomedia into the
 * recordings folder. That is what the SAF tree in [SafScanner] is for.
 */
@Suppress("DEPRECATION") // MediaStore.Audio.Media.DATA: still the only way
// to learn a real filesystem path, which FileObserver needs.
object MediaStoreQuery {

    private val PROJECTION = arrayOf(
        MediaStore.Audio.Media._ID,
        MediaStore.Audio.Media.DISPLAY_NAME,
        MediaStore.Audio.Media.SIZE,
        MediaStore.Audio.Media.DATE_MODIFIED,
        MediaStore.Audio.Media.MIME_TYPE,
        MediaStore.Audio.Media.RELATIVE_PATH,
        MediaStore.Audio.Media.DATA,
    )

    private fun collection(): Uri =
        MediaStore.Audio.Media.getContentUri(MediaStore.VOLUME_EXTERNAL)

    /**
     * Files whose RELATIVE_PATH starts with [relativePath].
     *
     * SQLite LIKE is case-insensitive for ASCII, which is a happy accident:
     * "sounds/callrecord/" and "Sounds/CallRecord/" both match.
     */
    fun listUnder(context: Context, relativePath: String, limit: Int = 400): List<RecordingRef> {
        val normalized = relativePath.trim('/').let { if (it.isEmpty()) "" else "$it/" }
        val out = ArrayList<RecordingRef>()
        val selection = "${MediaStore.Audio.Media.RELATIVE_PATH} LIKE ?"
        val args = arrayOf("$normalized%")
        val order = "${MediaStore.Audio.Media.DATE_MODIFIED} DESC"

        try {
            context.contentResolver.query(collection(), PROJECTION, selection, args, order)
                ?.use { c ->
                    val idIdx = c.getColumnIndexOrThrow(MediaStore.Audio.Media._ID)
                    val nameIdx = c.getColumnIndexOrThrow(MediaStore.Audio.Media.DISPLAY_NAME)
                    val sizeIdx = c.getColumnIndexOrThrow(MediaStore.Audio.Media.SIZE)
                    val modIdx = c.getColumnIndexOrThrow(MediaStore.Audio.Media.DATE_MODIFIED)
                    val mimeIdx = c.getColumnIndexOrThrow(MediaStore.Audio.Media.MIME_TYPE)
                    val dataIdx = c.getColumnIndex(MediaStore.Audio.Media.DATA)
                    while (c.moveToNext() && out.size < limit) {
                        val id = c.getLong(idIdx)
                        out.add(
                            RecordingRef(
                                uri = ContentUris.withAppendedId(collection(), id),
                                displayName = c.getString(nameIdx) ?: "recording-$id",
                                sizeBytes = c.getLong(sizeIdx),
                                // DATE_MODIFIED is seconds since epoch, not millis.
                                lastModifiedMillis = c.getLong(modIdx) * 1000L,
                                mimeType = c.getString(mimeIdx),
                                absolutePath = if (dataIdx >= 0) c.getString(dataIdx) else null,
                            )
                        )
                    }
                }
        } catch (t: Throwable) {
            // A missing permission surfaces here as a SecurityException. The
            // caller turns that into a red Health row, never a silent no-op.
            Logx.w("MediaStore query failed for '$normalized': ${t.message}")
        }
        return out
    }

    /** How many audio rows live under this path. Used by the folder probe. */
    fun countUnder(context: Context, relativePath: String): Int =
        listUnder(context, relativePath, limit = 5).size

    /**
     * Generation numbers (API 30+) are the documented way to detect MediaStore
     * deltas; DATE_MODIFIED is not, because any app can rewrite it and a wrong
     * system clock breaks it. Guarded by getVersion(): when the version string
     * changes, every generation number is meaningless and must be discarded.
     */
    fun generation(context: Context): Pair<String, Long>? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return null
        return try {
            val version = MediaStore.getVersion(context, MediaStore.VOLUME_EXTERNAL)
            val gen = MediaStore.getGeneration(context, MediaStore.VOLUME_EXTERNAL)
            version to gen
        } catch (t: Throwable) {
            Logx.w("MediaStore.getGeneration unavailable: ${t.message}")
            null
        }
    }
}
