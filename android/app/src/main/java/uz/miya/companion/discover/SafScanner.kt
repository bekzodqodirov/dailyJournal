package uz.miya.companion.discover

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import androidx.documentfile.provider.DocumentFile
import uz.miya.companion.data.RecordingRef
import uz.miya.companion.util.Logx

/**
 * The persisted-SAF-tree access path. It is immune to .nomedia, needs no
 * dangerous permission, and behaves identically on Android 10 through 16 —
 * which makes it the access path of last resort that always works, at the cost
 * of the owner hand-picking the folder once.
 */
object SafScanner {

    /**
     * Picker intent, pre-seeded at the folder we believe is right.
     * EXTRA_INITIAL_URI is a HINT the picker may ignore, never a guarantee —
     * and DocumentsUI flatly refuses anything under Android/data.
     */
    fun pickIntent(relativePathHint: String?): Intent {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
        intent.addFlags(
            Intent.FLAG_GRANT_READ_URI_PERMISSION or
                Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !relativePathHint.isNullOrBlank()) {
            try {
                val docId = "primary:" + relativePathHint.trim('/')
                val hint = DocumentsContract.buildDocumentUri(
                    "com.android.externalstorage.documents",
                    docId,
                )
                intent.putExtra(DocumentsContract.EXTRA_INITIAL_URI, hint)
            } catch (t: Throwable) {
                Logx.w("Could not build picker hint: ${t.message}")
            }
        }
        return intent
    }

    /** Take the grant so it survives reboot. Without this the URI dies on restart. */
    fun persist(context: Context, treeUri: Uri): Boolean = try {
        context.contentResolver.takePersistableUriPermission(
            treeUri,
            Intent.FLAG_GRANT_READ_URI_PERMISSION,
        )
        true
    } catch (t: Throwable) {
        Logx.e("takePersistableUriPermission failed", t)
        false
    }

    /** Is the grant we stored still ours? Checked on every scan and every launch. */
    fun stillGranted(context: Context, treeUri: Uri): Boolean =
        context.contentResolver.persistedUriPermissions.any {
            it.isReadPermission && it.uri == treeUri
        }

    /**
     * Enumerate audio files in the tree. One level of subdirectories is walked
     * because some OEMs bucket recordings by month; deeper than that is a sign
     * the wrong folder was picked and we should not crawl the whole card.
     */
    fun list(context: Context, treeUri: Uri, maxFiles: Int = 400): List<RecordingRef> {
        val out = ArrayList<RecordingRef>()
        val root = try {
            DocumentFile.fromTreeUri(context, treeUri)
        } catch (t: Throwable) {
            Logx.e("fromTreeUri failed", t)
            null
        } ?: return out

        fun collect(dir: DocumentFile, depth: Int) {
            if (out.size >= maxFiles) return
            val children = try {
                dir.listFiles()
            } catch (t: Throwable) {
                Logx.w("SAF listFiles failed: ${t.message}")
                emptyArray()
            }
            for (child in children) {
                if (out.size >= maxFiles) return
                if (child.isDirectory) {
                    if (depth < 1) collect(child, depth + 1)
                    continue
                }
                val name = child.name ?: continue
                if (!FolderProbe.isAudioName(name)) continue
                out.add(
                    RecordingRef(
                        uri = child.uri,
                        displayName = name,
                        sizeBytes = child.length(),
                        lastModifiedMillis = child.lastModified(),
                        mimeType = child.type,
                        absolutePath = null,
                    )
                )
            }
        }

        collect(root, 0)
        return out
    }
}
