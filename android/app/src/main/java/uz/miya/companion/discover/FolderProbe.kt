package uz.miya.companion.discover

import android.content.Context
import android.os.Environment
import android.telecom.TelecomManager
import uz.miya.companion.util.Logx
import uz.miya.companion.util.StorageAccess
import java.io.File

sealed class ProbeVerdict {
    /** A candidate folder actually contains audio. [relativePath] is MediaStore form. */
    data class Found(val relativePath: String, val sampleName: String?) : ProbeVerdict()

    /**
     * The default dialer is Google's. Its recordings live in app-private
     * storage that neither MANAGE_EXTERNAL_STORAGE nor SAF can reach on
     * Android 11+. Audio capture is impossible on this phone.
     */
    object GoogleDialerUnreachable : ProbeVerdict()

    /** Nothing found anywhere. Either no recorder, or no recording made yet. */
    object NoRecorder : ProbeVerdict()

    /** We cannot see shared storage at all yet — ask for the permission first. */
    object NeedsPermission : ProbeVerdict()

    fun label(): String = when (this) {
        is Found -> "Found: $relativePath"
        GoogleDialerUnreachable -> "Google Dialer (unreachable)"
        NoRecorder -> "No recordings found"
        NeedsPermission -> "Permission needed"
    }
}

/**
 * Onboarding's capability probe. Existence is decided via MediaStore, never via
 * File.exists() / File.listFiles(): under scoped storage an empty array is the
 * NORMAL result for a directory you lack visibility into, and branching on it
 * is how you conclude "no recorder" on a phone that records perfectly well.
 */
object FolderProbe {

    fun detect(context: Context, hasMediaPermission: Boolean): ProbeVerdict {
        if (!hasMediaPermission && !StorageAccess.allFiles()) {
            return ProbeVerdict.NeedsPermission
        }

        for (candidate in OemCandidates.ordered()) {
            val hits = MediaStoreQuery.listUnder(context, candidate, limit = 5)
                .filter { isAudioName(it.displayName) }
            if (hits.isNotEmpty()) {
                Logx.i("Probe matched $candidate (${hits.size} sample rows)")
                return ProbeVerdict.Found(candidate, hits.first().displayName)
            }
        }

        // MediaStore saw nothing. If we hold all-files access, a .nomedia in
        // the folder is the likely reason, so try the filesystem directly.
        if (StorageAccess.allFiles()) {
            for (candidate in OemCandidates.ordered()) {
                val dir = File(Environment.getExternalStorageDirectory(), candidate)
                val entries = dir.listFiles()?.filter { it.isFile && isAudioName(it.name) }.orEmpty()
                if (entries.isNotEmpty()) {
                    Logx.i("Probe matched $candidate via direct file listing")
                    return ProbeVerdict.Found(candidate, entries.first().name)
                }
            }
        }

        if (usesGoogleDialer(context)) return ProbeVerdict.GoogleDialerUnreachable
        return ProbeVerdict.NoRecorder
    }

    fun usesGoogleDialer(context: Context): Boolean = try {
        val tm = context.getSystemService(TelecomManager::class.java)
        tm?.defaultDialerPackage == OemCandidates.GOOGLE_DIALER_PACKAGE
    } catch (t: Throwable) {
        Logx.w("Cannot read default dialer package: ${t.message}")
        false
    }

    fun defaultDialerPackage(context: Context): String? = try {
        context.getSystemService(TelecomManager::class.java)?.defaultDialerPackage
    } catch (t: Throwable) {
        null
    }

    fun isAudioName(name: String?): Boolean {
        if (name.isNullOrBlank()) return false
        if (name.startsWith(".")) return false
        val ext = name.substringAfterLast('.', "").lowercase()
        if (ext.isEmpty()) return false
        if (ext in OemCandidates.REJECTED_EXTENSIONS) return false
        return ext in OemCandidates.AUDIO_EXTENSIONS
    }
}
