package uz.miya.companion.util

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Environment
import androidx.core.content.ContextCompat

/**
 * The three ways this app can be allowed to see a recording, checked in one
 * place so the Health screen and the scanner can never disagree.
 */
object StorageAccess {

    /** MANAGE_EXTERNAL_STORAGE. API 30+ only; on API 29 it does not exist. */
    fun allFiles(): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && Environment.isExternalStorageManager()

    /** READ_MEDIA_AUDIO on API 33+, READ_EXTERNAL_STORAGE below it. */
    fun mediaAudioPermission(): String =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            Manifest.permission.READ_MEDIA_AUDIO
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }

    fun hasMediaAudio(context: Context): Boolean = granted(context, mediaAudioPermission())

    fun granted(context: Context, permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED
}
