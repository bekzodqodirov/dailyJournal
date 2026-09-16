package uz.miya.companion.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import uz.miya.companion.data.UploadEntity
import uz.miya.companion.data.UploadState
import uz.miya.companion.util.TimeFmt

@Composable
fun QueueScreen(state: UiState, vm: MainViewModel) {
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        item {
            Row(
                Modifier.fillMaxWidth().padding(vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Text(
                        "${state.queueDepth} waiting, ${state.failedRows.size} failed",
                        style = MaterialTheme.typography.titleMedium,
                    )
                    Text(
                        "Last accepted ${TimeFmt.ago(state.lastUploadAt)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                TextButton(onClick = { vm.retryAll() }) { Text("Retry all") }
            }
        }

        if (state.rows.isEmpty()) {
            item {
                Text(
                    "Nothing here yet. Record a call with your phone's dialer; MIYA picks it " +
                        "up a few seconds after you hang up.",
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(vertical = 24.dp),
                )
            }
        }

        items(state.rows, key = { it.sha256 }) { row ->
            QueueRow(row) { vm.retry(row.sha256) }
        }

        item { Spacer(Modifier.height(24.dp)) }
    }
}

@Composable
private fun QueueRow(row: UploadEntity, onRetry: () -> Unit) {
    Card(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Column(Modifier.padding(14.dp)) {
            Text(
                row.displayName,
                style = MaterialTheme.typography.titleSmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                buildString {
                    append(stateLabel(row.state))
                    append(" · ")
                    append(TimeFmt.human(row.startedAtMillis))
                    append(" · ")
                    append("${row.sizeBytes / 1024} KB")
                    row.durationSeconds?.let { append(" · ${it}s") }
                },
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                buildString {
                    append(row.direction)
                    append(" · match: ${row.correlation}")
                    row.counterpartyName?.let { append(" · $it") }
                    if (row.attempts > 0) append(" · ${row.attempts} attempt(s)")
                },
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (!row.lastError.isNullOrBlank()) {
                Spacer(Modifier.height(6.dp))
                // The server's exact `detail` string, verbatim. Paraphrasing it
                // is how a fixable configuration error becomes a mystery.
                Text(
                    row.lastError,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
            if (row.state != UploadState.DONE && row.state != UploadState.SKIPPED) {
                OutlinedButton(onClick = onRetry) { Text("Retry now") }
            }
        }
    }
}

private fun stateLabel(state: String): String = when (state) {
    UploadState.PENDING -> "waiting"
    UploadState.UPLOADING -> "uploading"
    UploadState.DONE -> "sent"
    UploadState.FAILED_PERMANENT -> "failed"
    UploadState.FAILED_PRECONDITION -> "blocked"
    UploadState.SKIPPED -> "skipped"
    else -> state
}
