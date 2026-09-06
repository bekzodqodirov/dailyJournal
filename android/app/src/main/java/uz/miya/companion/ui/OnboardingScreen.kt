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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import uz.miya.companion.oem.OemHints

/**
 * The capability probe, run in order, each step blocking the next. It refuses
 * to claim the app is working until the steps that can be verified have been.
 *
 * Step 1 is not busywork: on a fresh phone the OEM recordings folder DOES NOT
 * EXIST until the first recorded call, and FileObserver reports nothing for a
 * directory that did not exist when it was registered — ever, even if the
 * directory appears later. Everything downstream needs the folder to be real.
 */
@Composable
fun OnboardingScreen(state: UiState, vm: MainViewModel, actions: UiActions) {
    val context = LocalContext.current
    val snackbar = remember { SnackbarHostState() }
    val prefs = state.prefs

    var url by remember(prefs?.serverUrl) { mutableStateOf(prefs?.serverUrl.orEmpty()) }
    var token by remember { mutableStateOf("") }

    LaunchedEffect(state.message) {
        state.message?.let {
            snackbar.showSnackbar(it)
            vm.clearMessage()
        }
    }

    Scaffold(snackbarHost = { SnackbarHost(snackbar) }) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(padding)
                .padding(bottom = 32.dp)
        ) {
            Surface(color = MaterialTheme.colorScheme.primaryContainer) {
                Column(Modifier.padding(16.dp)) {
                    Text("MIYA Companion", style = MaterialTheme.typography.headlineSmall)
                    Spacer(Modifier.height(6.dp))
                    Text(
                        "MIYA does not record calls. No app you install can — Android silences " +
                            "an ordinary app's microphone during a call, and the APIs that do " +
                            "work are reserved for apps built into the phone.\n\n" +
                            "What MIYA does is watch the folder your phone's OWN dialer writes " +
                            "recordings into, and send them to your server.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }

            StepCard(
                number = 1,
                title = "Turn on call recording, then record one test call",
                body = "Open your Phone app → Settings → \"Record calls\" (Samsung) or " +
                    "\"Call recording\" (Xiaomi). If that setting does not exist, stop: this " +
                    "phone cannot record calls and no app can change that.\n\n" +
                    "Then make one 10-second call and record it. The recordings folder does " +
                    "not exist until you do.",
            ) {}

            StepCard(
                number = 2,
                title = "Grant permissions",
                body = "Music & audio, notifications and phone state. MIYA will also ask for " +
                    "the call log — that one is hard-restricted on Android 10+ and is often " +
                    "refused for a sideloaded app. MIYA works without it, with less detail.\n\n" +
                    "MIYA never asks for microphone access, because it never records.",
            ) {
                Button(onClick = { actions.requestPermissions(defaultPermissionSet()) }) {
                    Text("Grant")
                }
            }

            StepCard(
                number = 3,
                title = "Find the recordings folder",
                body = state.prefs?.probeVerdict?.let { "Detection result: $it" }
                    ?: "Auto-detect looks for real audio files in the folders your brand of " +
                    "phone is known to use. If it finds nothing, pick the folder by hand.",
            ) {
                ButtonRow {
                    Button(onClick = { vm.runProbe() }, enabled = !state.busy) {
                        Text("Auto-detect")
                    }
                    OutlinedButton(onClick = { actions.pickFolder() }) { Text("Pick by hand") }
                }
            }

            StepCard(
                number = 4,
                title = "Disable battery optimisation",
                body = "This does double duty: it stops Doze deferring uploads, and it is one " +
                    "of the few exemptions that let MIYA start its short background burst when " +
                    "a call ends.",
            ) {
                Button(onClick = { actions.openIntent(OemHints.batteryExemptionIntent(context)) }) {
                    Text("Open setting")
                }
            }

            StepCard(
                number = 5,
                title = "Work the ${android.os.Build.MANUFACTURER} checklist",
                body = "Autostart, background autostart, \"no restrictions\", removal from " +
                    "sleeping apps — whichever your phone has. No app can set these for you, " +
                    "and OS updates commonly reset them.",
            ) {
                Column {
                    OemHints.steps(context).forEach { step ->
                        OutlinedButton(onClick = { OemHints.open(context, step) }) {
                            Text(step.title)
                        }
                    }
                }
            }

            StepCard(
                number = 6,
                title = "Server and token",
                body = "The URL is your VPS on the private tunnel, e.g. http://100.x.y.z:8000. " +
                    "The token is the one you put in UPLOAD_TOKENS on the server.",
            ) {
                Column {
                    OutlinedTextField(
                        value = url,
                        onValueChange = { url = it },
                        label = { Text("Server URL") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = token,
                        onValueChange = { token = it },
                        label = { Text("Bearer token") },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(8.dp))
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
                    }
                }
            }

            StepCard(
                number = 7,
                title = "Make one more test call",
                body = "Record it, then watch the Queue screen. If it does not turn up, the " +
                    "Health screen names the step that is failing.",
            ) {
                Button(onClick = { vm.completeOnboarding() }) { Text("Finish setup") }
            }

            Spacer(Modifier.height(16.dp))
            Text(
                "One legal note, once: most phones announce \"this call is being recorded\" to " +
                    "the other person, and recording both sides of a call is restricted in many " +
                    "countries. That is your dialer's behaviour and your decision — MIYA cannot " +
                    "change either.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 16.dp),
            )
        }
    }
}

@Composable
private fun StepCard(
    number: Int,
    title: String,
    body: String,
    content: @Composable () -> Unit,
) {
    SectionCard("$number. $title") {
        Text(
            body,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(10.dp))
        content()
    }
}
