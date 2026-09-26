package uz.miya.companion.oem

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import uz.miya.companion.util.Logx

/**
 * OEM process killers are a SEPARATE and larger problem than AOSP Doze, and no
 * manifest entry fixes any of them. Worse, Samsung and Xiaomi commonly RESET
 * these toggles on an OS update — so this checklist is re-shown on every
 * launch rather than once at onboarding, which is the classic mistake.
 *
 * Reference: https://dontkillmyapp.com
 */
object OemHints {

    data class Step(val title: String, val detail: String, val intents: List<Intent>)

    private fun component(pkg: String, cls: String): Intent =
        Intent().setComponent(ComponentName(pkg, cls))

    private fun appDetails(context: Context): Intent =
        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
            .setData(Uri.fromParts("package", context.packageName, null))

    fun manufacturer(): String = Build.MANUFACTURER.lowercase()

    fun steps(context: Context): List<Step> {
        val brand = (Build.MANUFACTURER + " " + Build.BRAND).lowercase()
        val fallback = listOf(appDetails(context))

        return when {
            listOf("xiaomi", "redmi", "poco").any { brand.contains(it) } -> listOf(
                Step(
                    "Autostart: ON",
                    "Security → Permissions → Autostart → enable MIYA Companion. " +
                        "On MIUI 14 / HyperOS also enable \"Background autostart\".",
                    listOf(
                        component(
                            "com.miui.securitycenter",
                            "com.miui.permcenter.autostart.AutoStartManagementActivity",
                        ),
                    ) + fallback,
                ),
                Step(
                    "Battery saver: No restrictions",
                    "Settings → Apps → MIYA → Battery saver → No restrictions.",
                    listOf(
                        component("com.miui.powerkeeper", "com.miui.powerkeeper.ui.HiddenAppsConfigActivity"),
                    ) + fallback,
                ),
                Step(
                    "Lock in Recents",
                    "Open Recents, pull MIYA down (or tap the lock) so the cleaner skips it.",
                    fallback,
                ),
            )

            brand.contains("samsung") -> listOf(
                Step(
                    "Not a sleeping app",
                    "Settings → Battery → Background usage limits → remove MIYA from " +
                        "\"Sleeping apps\" AND \"Deep sleeping apps\".",
                    listOf(
                        component(
                            "com.samsung.android.lool",
                            "com.samsung.android.sm.ui.battery.BatteryActivity",
                        ),
                    ) + fallback,
                ),
                Step(
                    "Adaptive battery off for MIYA",
                    "Settings → Apps → MIYA → Battery → Unrestricted.",
                    fallback,
                ),
            )

            listOf("huawei", "honor").any { brand.contains(it) } -> listOf(
                Step(
                    "Manage manually, all three toggles",
                    "Phone Manager → App launch → MIYA → Manage manually → " +
                        "Auto-launch, Secondary launch and Run in background all ON.",
                    listOf(
                        component(
                            "com.huawei.systemmanager",
                            "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity",
                        ),
                    ) + fallback,
                ),
            )

            listOf("oppo", "realme", "oneplus").any { brand.contains(it) } -> listOf(
                Step(
                    "Allow auto-startup",
                    "Settings → App management → MIYA → Allow auto startup, and set " +
                        "battery usage to \"Allow background running\".",
                    listOf(
                        component(
                            "com.coloros.safecenter",
                            "com.coloros.safecenter.permission.startup.StartupAppListActivity",
                        ),
                    ) + fallback,
                ),
            )

            listOf("vivo", "iqoo").any { brand.contains(it) } -> listOf(
                Step(
                    "High background power consumption: allow",
                    "i Manager → App manager → Autostart → enable MIYA, and Battery → " +
                        "High background power consumption → allow MIYA.",
                    listOf(component("com.iqoo.secure", "com.iqoo.secure.MainActivity")) + fallback,
                ),
            )

            listOf("tecno", "infinix", "itel").any { brand.contains(it) } -> listOf(
                Step(
                    "Phone Master: protected app",
                    "Phone Master → App management → Auto start → enable MIYA.",
                    fallback,
                ),
            )

            else -> listOf(
                Step(
                    "Allow background activity",
                    "Settings → Apps → MIYA → Battery → Unrestricted, and allow " +
                        "auto-start if your phone has that setting.",
                    fallback,
                ),
            )
        }
    }

    /** Launch the first intent that actually resolves on this device. */
    fun open(context: Context, step: Step) {
        for (intent in step.intents) {
            try {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                context.startActivity(intent)
                return
            } catch (t: Throwable) {
                Logx.d("OEM intent not available: ${intent.component}")
            }
        }
        Logx.w("No OEM settings screen resolved for '${step.title}'")
    }

    fun batteryExemptionIntent(context: Context): Intent =
        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:${context.packageName}"))

    fun allFilesAccessIntent(context: Context): Intent =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                .setData(Uri.parse("package:${context.packageName}"))
        } else {
            appDetails(context)
        }

    fun appDetailsIntent(context: Context): Intent = appDetails(context)
}
