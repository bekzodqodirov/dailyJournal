package uz.miya.companion.data

import android.net.Uri

/**
 * A candidate recording, wherever it came from: a MediaStore row, a SAF tree
 * entry, a real java.io.File (all-files mode), or a copy we made ourselves
 * when the owner shared a file into the app.
 *
 * Everything downstream reads bytes through ContentResolver.openInputStream(),
 * which handles content:// and file:// identically, so the rest of the app
 * never has to care which of the four it is.
 */
data class RecordingRef(
    val uri: Uri,
    val displayName: String,
    val sizeBytes: Long,
    val lastModifiedMillis: Long,
    val mimeType: String?,
    /** Known only for MediaStore rows (DATA) and File-mode scans. */
    val absolutePath: String?,
    /** True only for copies this app made and therefore may delete. */
    val ownedByUs: Boolean = false,
) {
    val key: String get() = uri.toString()
}
