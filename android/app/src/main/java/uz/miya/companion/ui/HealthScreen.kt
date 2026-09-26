package uz.miya.companion.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import uz.miya.companion.oem.OemHints
import uz.miya.companion.util.StorageAccess
import uz.miya.companion.util.TimeFmt

/**
 * The permanent honesty panel. Every prerequisite, its real state, re-checked
 * on every resume, each red row deep-linking to the screen that fixes it.
 */
@Composable
fun HealthScreen(
    state: UiState,
    vm: MainViewModel,
    actions: UiActions,
    goToSettings: () -> Unit,
) {
    val context = LocalContext.current
    val prefs = state.prefs

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(bottom = 24.dp)
    ) {
        if (prefs?.authFailed == true) {
            Surface(
                color = MaterialTheme.colorScheme.errorContainer,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    "The server rejected the bearer token (401). Uploads are stopped until you " +
                        "fix it in Settings.",
                    modifier = Modifier.padding(16.dp),
                    color = MaterialTheme.colorScheme.onErrorContainer,
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }

        SectionCard("Status") {
            KeyValue("Last accepted", TimeFmt.ago(state.lastUploadAt))
            KeyValue("Queue depth", state.queueDepth.toString())
            KeyValue("Failed", state.failedRows.size.toString())
            KeyValue("Last scan", TimeFmt.ago(prefs?.lastScanAt))
            KeyValue(
                "Call-end trigger",
                if (state.callTriggerActive) "active" else "inactive (needs phone state)",
            )
            KeyValue(
                "Watching",
                when {
                    state.watchedDirs.isNotEmpty() -> state.watchedDirs.joinToString("\n")
                    // Not a fault, and not fixable by anything the owner can do
                    // short of granting All files access: inotify needs real
                    // read access to the path, which scoped storage does not
                    // give. Saying "no readable folder yet" made a normal,
                    // working install look broken.
                    !StorageAccess.allFiles() ->
                        "inotify unavailable without All files access — using call-end, " +
                            "MediaStore and the 15-minute sweep"
                    else -> "no readable folder yet"
                },
            )
            if (!prefs?.lastError.isNullOrBlank()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Last error (${TimeFmt.ago(prefs?.lastErrorAt)}): ${prefs?.lastError}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
            Spacer(Modifier.height(12.dp))
            ButtonRow {
                Button(onClick = { vm.scanNow() }, enabled = !state.busy) { Text("Scan now") }
                OutlinedButton(onClick = { vm.runProbe() }, enabled = !state.busy) {
                    Text("Re-detect folder")
                }
            }
        }

        SectionCard("Prerequisites") {
            state.health.forEachIndexed { index, item ->
                if (index > 0) HorizontalDivider()
                HealthRow(item) {
                    handleHealthAction(item, vm, actions, context, goToSettings)
                }
            }
        }

        SectionCard("Keep MIYA alive on ${android.os.Build.MANUFACTURER}") {
            Text(
                "No manifest entry fixes OEM process killers, and Samsung and Xiaomi reset " +
                    "these toggles on OS updates. Re-check after every system update.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(8.dp))
            OemHints.steps(context).forEach { step ->
                Column(Modifier.padding(vertical = 6.dp)) {
                    Text(step.title, style = MaterialTheme.typography.titleSmall)
                    Text(
                        step.detail,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    OutlinedButton(onClick = { OemHints.open(context, step) }) { Text("Open") }
                }
            }
        }

        SectionCard("What MIYA cannot do") {
            Text(
                "MIYA never records anything. It reads the files your phone's own dialer " +
                    "writes. If your dialer cannot record calls, or it is Google Dialer, no " +
                    "app on an unrooted phone can capture that audio — including this one.\n\n" +
                    "It cannot capture WhatsApp or Telegram calls.\n\n" +
                    "It cannot stop your dialer announcing \"this call is being recorded\" to " +
                    "the other person. That is your dialer, not MIYA.\n\n" +
                    "It guarantees eventual delivery, never timely delivery. Best case is one " +
                    "to two minutes; offline or under Doze it can be hours.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}
