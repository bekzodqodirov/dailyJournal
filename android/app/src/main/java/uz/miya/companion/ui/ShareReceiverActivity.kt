package uz.miya.companion.ui

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import uz.miya.companion.Graph
import uz.miya.companion.util.Logx
import uz.miya.companion.work.Scheduling
import java.io.File
import java.util.UUID

/**
 * "Share to MIYA". On a Google Dialer phone this is the ONLY path that works:
 * the recordings live in app-private storage no app can read, so the owner
 * shares each one out of the Phone app by hand. It is also the manual escape
 * hatch for anything the watcher missed.
 *
 * The shared content URI carries a grant tied to THIS activity instance, so the
 * bytes are copied into our own files directory before the activity finishes,
 * and the queue points at that copy. Queueing the original URI — or copying
 * after finish() — produces a row that can never be read again.
 */
class ShareReceiverActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Graph.init(applicationContext)

        val uris = when (intent?.action) {
            Intent.ACTION_SEND -> listOfNotNull(extra(intent, Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE -> extraList(intent, Intent.EXTRA_STREAM)
            else -> emptyList()
        }

        if (uris.isEmpty()) {
            toast("Nothing to import.")
            finish()
            return
        }

        toast("Importing ${uris.size} recording(s)…")

        lifecycleScope.launch {
            val copied = withContext(Dispatchers.IO) {
                uris.count { importOne(it) }
            }
            toast(
                if (copied == 0) "Could not read the shared file."
                else "Imported $copied recording(s). MIYA will upload them shortly."
            )
            Scheduling.enqueueImmediateScan(applicationContext)
            finish()
        }
    }

    private fun importOne(uri: Uri): Boolean = try {
        val name = displayName(uri) ?: "shared-${System.currentTimeMillis()}.m4a"
        val dir = File(filesDir, SHARED_DIR).apply { mkdirs() }
        // "__" separator, and no dashes in the id, so RecordingScanner can strip
        // the prefix back off and send the dialer's own filename as provenance.
        val id = UUID.randomUUID().toString().replace("-", "")
        val target = File(dir, id + SEPARATOR + name)
        contentResolver.openInputStream(uri)?.use { input ->
            target.outputStream().use { output -> input.copyTo(output, 1 shl 16) }
        } ?: throw IllegalStateException("openInputStream returned null")
        Logx.i("Imported shared recording as ${Logx.redactName(target.name)}")
        true
    } catch (t: Throwable) {
        Logx.e("Share import failed", t)
        false
    }

    private fun displayName(uri: Uri): String? = try {
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
            ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
            ?.replace('/', '_')
    } catch (t: Throwable) {
        null
    }

    @Suppress("DEPRECATION")
    private fun extra(intent: Intent, key: String): Uri? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(key, Uri::class.java)
        } else {
            intent.getParcelableExtra(key)
        }

    @Suppress("DEPRECATION")
    private fun extraList(intent: Intent, key: String): List<Uri> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableArrayListExtra(key, Uri::class.java).orEmpty()
        } else {
            intent.getParcelableArrayListExtra<Uri>(key).orEmpty()
        }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    companion object {
        const val SHARED_DIR = "shared"
        const val SEPARATOR = "__"
    }
}
