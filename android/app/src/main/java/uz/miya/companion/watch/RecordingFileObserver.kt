package uz.miya.companion.watch

import android.os.FileObserver
import uz.miya.companion.util.Logx
import java.io.File

/**
 * inotify on the recordings directories.
 *
 * Three things about FileObserver decide whether this works at all:
 *
 *  1. "The monitored file or directory must exist at this time, or else no
 *     events will be reported (even if it appears later)." OEM recording
 *     folders do not exist until the first recording, so [WatchArmer] also
 *     watches the nearest existing ancestor and re-arms.
 *  2. "If a FileObserver is garbage collected, it will stop sending events."
 *     Holding one in a local is the classic bug that works in debug and dies
 *     in release; MiyaApp holds the strong reference.
 *  3. Events arrive on a dedicated FileObserver thread with no synchronisation,
 *     so nothing is done here beyond handing the event straight off.
 *
 * The mask is CLOSE_WRITE | MOVED_TO and never CREATE or MODIFY: Samsung
 * writes a .3ga during the call and produces the .m4a at the end, and several
 * OEMs write to a temp name and rename.
 */
class RecordingFileObserver(
    dirs: List<File>,
    mask: Int,
    private val handler: (Int, String?) -> Unit,
) : FileObserver(dirs, mask) {

    override fun onEvent(event: Int, path: String?) {
        try {
            handler(event, path)
        } catch (t: Throwable) {
            Logx.w("FileObserver handler threw", t)
        }
    }

    companion object {
        /** Finished files only. Explicitly qualified, and `val` not `const val`,
         *  because `or` is a function call and not a compile-time constant. */
        val MASK_FILES: Int = FileObserver.CLOSE_WRITE or FileObserver.MOVED_TO

        /** The recordings folder itself appearing for the first time. */
        val MASK_DIRS: Int = FileObserver.CREATE or FileObserver.MOVED_TO
    }
}
