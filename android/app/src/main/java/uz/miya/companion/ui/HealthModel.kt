package uz.miya.companion.ui

enum class HealthAction {
    NONE,
    NOTIFICATION_PERMISSION,
    MEDIA_PERMISSION,
    PHONE_STATE_PERMISSION,
    CALL_LOG_PERMISSION,
    PICK_FOLDER,
    RUN_PROBE,
    BATTERY,
    ALL_FILES,
    OEM,
    SERVER_SETTINGS,
    SCAN_NOW,
}

/**
 * One row of the permanent honesty panel. `critical = false` means the app
 * still works without it, just with less information — READ_CALL_LOG being the
 * important example, because on a sideloaded APK it may be ungrantable
 * forever and the app must never present that as broken.
 */
data class HealthItem(
    val title: String,
    val ok: Boolean,
    val detail: String,
    val action: HealthAction = HealthAction.NONE,
    val actionLabel: String? = null,
    val critical: Boolean = true,
)
