package uz.miya.companion.ui

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import uz.miya.companion.discover.SafScanner
import uz.miya.companion.oem.OemHints
import uz.miya.companion.util.Logx
import uz.miya.companion.work.Scheduling

class MainActivity : ComponentActivity() {

    private val vm: MainViewModel by viewModels()

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { vm.refresh() }

    /**
     * Deliberately StartActivityForResult with our own intent rather than the
     * OpenDocumentTree contract: we need FLAG_GRANT_PERSISTABLE_URI_PERMISSION
     * and EXTRA_INITIAL_URI set explicitly, so the picker opens on the folder
     * we already believe is right.
     */
    private val treeLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val uri = result.data?.data
        if (uri != null) vm.onFolderPicked(uri) else vm.refresh()
    }

    private val settingsLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { vm.refresh() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // One-shot reconciliation on app open, alongside boot and the
        // 15-minute sweep. Opening the app is also what resets the Android 15
        // foreground-service time budget, so it is a good moment to look.
        Scheduling.enqueueImmediateScan(this)

        setContent {
            MiyaTheme {
                val state by vm.state.collectAsStateWithLifecycle()
                val actions = remember {
                    UiActions(
                        requestPermissions = { permissionLauncher.launch(it) },
                        pickFolder = {
                            treeLauncher.launch(SafScanner.pickIntent(vm.folderHint()))
                        },
                        openIntent = { intent ->
                            try {
                                settingsLauncher.launch(intent)
                            } catch (t: Throwable) {
                                Logx.w("Cannot open ${intent.action}: ${t.message}")
                            }
                        },
                    )
                }
                val prefs = state.prefs
                when {
                    prefs == null -> Unit // first frame, before DataStore reads
                    prefs.onboardingComplete -> MainScaffold(state, vm, actions)
                    else -> OnboardingScreen(state, vm, actions)
                }
            }
        }
    }

    // Health is re-verified on every resume, never once at onboarding.
    override fun onResume() {
        super.onResume()
        vm.refresh()
    }
}

/** System-level actions the ViewModel cannot perform on its own. */
class UiActions(
    val requestPermissions: (Array<String>) -> Unit,
    val pickFolder: () -> Unit,
    val openIntent: (Intent) -> Unit,
)

fun defaultPermissionSet(): Array<String> {
    val list = ArrayList<String>()
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        list.add(Manifest.permission.READ_MEDIA_AUDIO)
        list.add(Manifest.permission.POST_NOTIFICATIONS)
    } else {
        list.add(Manifest.permission.READ_EXTERNAL_STORAGE)
    }
    list.add(Manifest.permission.READ_PHONE_STATE)
    list.add(Manifest.permission.READ_CONTACTS)
    // Hard-restricted: this request is very likely to be auto-denied on a
    // sideloaded build. Asking costs nothing and the app works without it.
    list.add(Manifest.permission.READ_CALL_LOG)
    return list.toTypedArray()
}

fun handleHealthAction(
    item: HealthItem,
    vm: MainViewModel,
    actions: UiActions,
    context: android.content.Context,
    goToSettings: () -> Unit,
) {
    when (item.action) {
        HealthAction.NOTIFICATION_PERMISSION ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                actions.requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS))
            }
        HealthAction.MEDIA_PERMISSION ->
            actions.requestPermissions(
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    arrayOf(Manifest.permission.READ_MEDIA_AUDIO)
                } else {
                    arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE)
                }
            )
        HealthAction.PHONE_STATE_PERMISSION ->
            actions.requestPermissions(arrayOf(Manifest.permission.READ_PHONE_STATE))
        HealthAction.CALL_LOG_PERMISSION ->
            actions.requestPermissions(arrayOf(Manifest.permission.READ_CALL_LOG))
        HealthAction.PICK_FOLDER -> actions.pickFolder()
        HealthAction.RUN_PROBE -> vm.runProbe()
        HealthAction.BATTERY -> actions.openIntent(OemHints.batteryExemptionIntent(context))
        HealthAction.ALL_FILES -> actions.openIntent(OemHints.allFilesAccessIntent(context))
        HealthAction.OEM -> actions.openIntent(OemHints.appDetailsIntent(context))
        HealthAction.SERVER_SETTINGS -> goToSettings()
        HealthAction.SCAN_NOW -> vm.scanNow()
        HealthAction.NONE -> Unit
    }
}

private enum class Tab { HEALTH, QUEUE, SETTINGS }

@Composable
private fun MainScaffold(state: UiState, vm: MainViewModel, actions: UiActions) {
    var tab by remember { mutableStateOf(Tab.HEALTH) }
    val snackbar = remember { SnackbarHostState() }

    LaunchedEffect(state.message) {
        val message = state.message
        if (message != null) {
            snackbar.showSnackbar(message)
            vm.clearMessage()
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        bottomBar = {
            NavigationBar {
                NavigationBarItem(
                    selected = tab == Tab.HEALTH,
                    onClick = { tab = Tab.HEALTH },
                    icon = { Icon(Icons.Filled.CheckCircle, contentDescription = null) },
                    label = { Text("Health") },
                )
                NavigationBarItem(
                    selected = tab == Tab.QUEUE,
                    onClick = { tab = Tab.QUEUE },
                    icon = { Icon(Icons.Filled.List, contentDescription = null) },
                    label = { Text("Queue (${state.queueDepth})") },
                )
                NavigationBarItem(
                    selected = tab == Tab.SETTINGS,
                    onClick = { tab = Tab.SETTINGS },
                    icon = { Icon(Icons.Filled.Settings, contentDescription = null) },
                    label = { Text("Settings") },
                )
            }
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            when (tab) {
                Tab.HEALTH -> HealthScreen(state, vm, actions) { tab = Tab.SETTINGS }
                Tab.QUEUE -> QueueScreen(state, vm)
                Tab.SETTINGS -> SettingsScreen(state, vm, actions)
            }
        }
    }
}
