package uz.miya.companion.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import uz.miya.companion.oem.OemHints
import uz.miya.companion.util.StorageAccess

@Composable
fun SettingsScreen(state: UiState, vm: MainViewModel, actions: UiActions) {
    val context = LocalContext.current
    val prefs = state.prefs ?: return

    var url by remember(prefs.serverUrl) { mutableStateOf(prefs.serverUrl) }
    var token by remember { mutableStateOf("") }
    var language by remember(prefs.languageHint) { mutableStateOf(prefs.languageHint.orEmpty()) }
    var minDuration by remember(prefs.minDurationSeconds) {
        mutableStateOf(prefs.minDurationSeconds.toString())
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(bottom = 32.dp)
    ) {
        SectionCard("Server") {
            OutlinedTextField(
                value = url,
                onValueChange = { url = it },
                label = { Text("Server URL") },
                placeholder = { Text("http://100.x.y.z:8000") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = {
                    Text(
                        if (state.tokenSet) "Bearer token (saved: ${state.tokenMasked})"
                        else "Bearer token"
                    )
                },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "Stored encrypted with a key held in the Android KeyStore. It is never logged " +
                    "and never leaves the phone except as an Authorization header.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(12.dp))
            ButtonRow {
                Button(
                    onClick = {
                        vm.saveServer(url, token.takeIf { it.isNotBlank() })
                        token = ""
                    },
                ) { Text("Save") }
                OutlinedButton(onClick = { vm.testConnection() }, enabled = !state.busy) {
                    Text("Test connection")
                }
                if (state.tokenSet) {
                    OutlinedButton(onClick = { vm.clearToken() }) { Text("Forget token") }
                }
            }
        }

        SectionCard("Recordings folder") {
            KeyValue("Detected", prefs.probeVerdict ?: "not run yet")
            KeyValue("Watching", prefs.folderRelativePath?.let { "/$it" } ?: "none")
            KeyValue("Folder grant", if (prefs.treeUri != null) "held" else "none")
            KeyValue("All-files access", if (StorageAccess.allFiles()) "on" else "off")
            Spacer(Modifier.height(12.dp))
            ButtonRow {
                OutlinedButton(onClick = { vm.runProbe() }, enabled = !state.busy) {
                    Text("Auto-detect")
                }
                OutlinedButton(onClick = { actions.pickFolder() }) { Text("Pick folder") }
            }
            Spacer(Modifier.height(8.dp))
            Text(
                "Advanced: \"All files access\" lets MIYA scan every candidate folder without " +
                    "you picking one, including folders hidden from the media scanner by a " +
                    ".nomedia file. It still cannot reach Android/data, so it does not help on " +
                    "a Google Dialer phone.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            OutlinedButton(onClick = { actions.openIntent(OemHints.allFilesAccessIntent(context)) }) {
                Text("All files access")
            }
        }

        SectionCard("Uploads") {
            ToggleRow(
                title = "Upload on Wi-Fi only",
                subtitle = "Off means recordings also go out over mobile data.",
                checked = prefs.wifiOnly,
                onChange = vm::setWifiOnly,
            )
            ToggleRow(
                title = "Delete local copy after upload",
                subtitle = "Off by default. Deleting your dialer's own recordings is not " +
                    "MIYA's business unless you say so.",
                checked = prefs.deleteAfterUpload,
                onChange = vm::setDeleteAfterUpload,
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = language,
                onValueChange = { language = it },
                label = { Text("Language hint (e.g. uz, ru, en)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = minDuration,
                onValueChange = { minDuration = it.filter(Char::isDigit).take(3) },
                label = { Text("Ignore calls shorter than (seconds)") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "Misdials cost real money to transcribe. 4 seconds is a sensible floor.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(12.dp))
            Button(
                onClick = {
                    vm.setLanguageHint(language)
                    vm.setMinDuration(minDuration.toIntOrNull() ?: 4)
                },
            ) { Text("Save preferences") }
        }

        SectionCard("Device") {
            KeyValue("Device id", prefs.deviceId.take(8) + "…")
            KeyValue("Dialer", uz.miya.companion.discover.FolderProbe.defaultDialerPackage(context) ?: "unknown")
            Spacer(Modifier.height(8.dp))
            Text(
                "The device id is half of the call_id the server de-duplicates on. It is " +
                    "generated once and never changes.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(12.dp))
            OutlinedButton(onClick = { actions.openIntent(OemHints.appDetailsIntent(context)) }) {
                Text("App info")
            }
        }
    }
}

@Composable
private fun ToggleRow(
    title: String,
    subtitle: String,
    checked: Boolean,
    onChange: (Boolean) -> Unit,
) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
