package uz.miya.companion.watch

import android.content.Context
import android.database.ContentObserver
import android.net.Uri
import android.os.Handler
import android.os.HandlerThread
import android.provider.MediaStore
import uz.miya.companion.util.Logx

/**
 * A second net under the FileObserver: MediaProvider notifies when the media
 * scanner indexes a new audio file.
 *
 * Two facts shape this:
 *  - notifyForDescendants MUST be true, or you get nothing at all.
 *  - onChange says only "something changed" — it carries no delta and it can
 *    lag the write by seconds to minutes, and never fires at all if the folder
 *    has a .nomedia. So it triggers a scan; it never decides anything.
 */
class MediaStoreWatcher(
    private val context: Context,
    private val onChanged: () -> Unit,
) {

    private var thread: HandlerThread? = null
    private var observer: ContentObserver? = null

    fun register(): Boolean = try {
        unregister()
        val t = HandlerThread("miya-mediastore").apply { start() }
        thread = t
        val obs = object : ContentObserver(Handler(t.looper)) {
            override fun onChange(selfChange: Boolean, uri: Uri?) {
                onChanged()
            }
        }
        observer = obs
        context.contentResolver.registerContentObserver(
            MediaStore.Audio.Media.EXTERNAL_CONTENT_URI,
            /* notifyForDescendants = */ true,
            obs,
        )
        true
    } catch (t: Throwable) {
        Logx.e("Cannot register MediaStore observer", t)
        false
    }

    fun unregister() {
        observer?.let {
            try {
                context.contentResolver.unregisterContentObserver(it)
            } catch (t: Throwable) {
                Logx.w("unregisterContentObserver failed", t)
            }
        }
        observer = null
        thread?.quitSafely()
        thread = null
    }
}
