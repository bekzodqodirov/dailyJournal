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

    /** Call-log events (build step 6) and recording correlation both hang on it. */
    fun hasCallLog(context: Context): Boolean =
        granted(context, Manifest.permission.READ_CALL_LOG)

    /**
     * SMS upload needs both halves: RECEIVE_SMS for the wake-up broadcast and
     * READ_SMS for the provider the worker actually reads. Both live in the
     * same hard-restricted permission group as READ_CALL_LOG, so a sideloaded
     * APK may never be able to hold them — the app degrades, never breaks.
     */
    fun hasSms(context: Context): Boolean =
        granted(context, Manifest.permission.READ_SMS) &&
            granted(context, Manifest.permission.RECEIVE_SMS)

    fun granted(context: Context, permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED
}
