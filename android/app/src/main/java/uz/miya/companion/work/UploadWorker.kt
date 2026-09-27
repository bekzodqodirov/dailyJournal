package uz.miya.companion.work

import android.content.Context
import android.content.pm.ServiceInfo
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import uz.miya.companion.Graph
import uz.miya.companion.data.UploadState
import uz.miya.companion.ingest.Hasher
import uz.miya.companion.ingest.StabilityGate
import uz.miya.companion.net.UploadOutcome
import uz.miya.companion.util.Logx
import uz.miya.companion.util.Notifications

/**
 * One upload attempt per run. WorkManager owns the retry schedule
 * (exponential, 30 s base, capped at 5 h) and the network constraint, so this
 * class only has to map a server response onto queue state — and it must map
 * BOTH 202 and 200 onto success, because treating a duplicate as a failure is
 * how a flaky network turns into re-uploading the owner's entire call history
 * every night.
 */
class UploadWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {

    override suspend fun getForegroundInfo(): ForegroundInfo =
        ForegroundInfo(
            Notifications.ID_SERVICE,
            Notifications.serviceNotification(applicationContext, "Uploading a recording…"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
        )

    override suspend fun doWork(): Result {
        Graph.init(applicationContext)
        val sha = inputData.getString(KEY_SHA) ?: return Result.failure()

        val repo = Graph.repository
        val prefs = Graph.prefs

        val row = repo.byHash(sha) ?: run {
            Logx.d("Row $sha no longer in queue; nothing to do")
            return Result.success()
        }
        if (row.state == UploadState.DONE) return Result.success()

        val snapshot = prefs.snapshot()

        // Nearly every first install hits this: the app finds the test-call
        // recording at onboarding step 4-5, long before the server URL and
        // token are entered at step 10. The row is parked as a precondition
        // failure rather than retried, and is released again by
        // MainViewModel.saveServer()/testConnection() (which call
        // retryAllFailed) and by DrainWorker, which now re-arms
        // FAILED_PRECONDITION rows on every app start and boot. Without one of
        // those, every pre-existing recording on the phone stayed blocked
        // forever behind a manual "Retry all" tap.
        if (!snapshot.serverConfigured || !Graph.tokenStore.hasToken()) {
            repo.markPrecondition(sha, "Server URL or token not configured yet")
            return Result.failure()
        }

        // §4.2: a 401 latches. Retrying a bad token forever is how you get a
        // phone that looks busy and does nothing.
        if (snapshot.authFailed) {
            Logx.w("Auth is known-bad; not attempting upload")
            return Result.failure()
        }

        // Read the device id through Prefs rather than the snapshot: it is
        // generated on first use, and device_id is a REQUIRED contract field.
        val deviceId = prefs.deviceId()
        val ref = repo.refFor(row)

        // The bytes must still be the bytes we hashed. One stat call, and it
        // converts "the OEM rewrote the recording after we hashed it" from a
        // Content-Length mismatch that looks like a network error (and retries
        // for ever) into a precondition the next scan repairs by re-hashing.
        val currentSize = StabilityGate.currentSize(applicationContext, ref.uri)
        if (currentSize < 0L) {
            repo.markPrecondition(sha, "Recording is no longer readable")
            return Result.failure()
        }
        if (currentSize != row.sizeBytes) {
            repo.markPrecondition(
                sha,
                "Recording changed on disk (${row.sizeBytes} -> $currentSize bytes); " +
                    "the next scan will re-hash it",
            )
            return Result.failure()
        }

        val attempt = runAttemptCount + 1
        repo.markUploading(sha, attempt)

        // §2.3 probe: a lost 202 should cost one small JSON round trip, not
        // 40 MB over a 3G link.
        Graph.api.probe(
            snapshot.serverUrl,
            listOf(sha),
            listOfNotNull(row.callId),
        ).onSuccess { probe ->
            val knownByHash = sha in probe.knownSha256
            val knownByCall = row.callId != null && row.callId in probe.knownCallId
            if (knownByHash || knownByCall) {
                Logx.i("Server already has ${Logx.shortSha(sha)}; marking done")
                repo.markDone(sha)
                repo.deleteSourceIfRequested(row)
                return Result.success()
            }
        }.onFailure { t ->
            // A failed probe is not a failed upload. Fall through and try.
            Logx.d("Probe failed (${t.message}); uploading anyway")
        }

        return when (val outcome = Graph.api.upload(snapshot.serverUrl, row, ref, deviceId)) {
            is UploadOutcome.Accepted, is UploadOutcome.Duplicate -> {
                repo.markDone(sha)
                repo.deleteSourceIfRequested(row)
                Notifications.cancel(applicationContext, Notifications.ID_STALLED)
                Result.success()
            }

            is UploadOutcome.AuthFailed -> {
                prefs.setAuthFailed(true)
                repo.markPermanent(sha, attempt, "401: ${outcome.detail}")
                Notifications.alert(
                    applicationContext,
                    Notifications.ID_STALLED,
                    "MIYA cannot sign in",
                    "The server rejected the bearer token. Open MIYA → Settings and paste it again.",
                )
                Result.failure()
            }

            is UploadOutcome.HashMismatch -> {
                if (row.hashRetried) {
                    repo.markPermanent(sha, attempt, "sha256 mismatch twice: ${outcome.detail}")
                    Result.failure()
                } else {
                    val fresh = runCatching { Hasher.sha256(applicationContext, ref.uri) }.getOrNull()
                    if (fresh == null) {
                        repo.markPrecondition(sha, "Recording is no longer readable")
                        Result.failure()
                    } else {
                        val replaced = repo.rehash(row, fresh.sha256, fresh.sizeBytes)
                        if (replaced == null) {
                            // Same hash as before: the corruption is on the wire.
                            repo.markRetry(sha, attempt, "sha256 mismatch: ${outcome.detail}")
                            Result.retry()
                        } else {
                            Logx.i("Re-hashed ${Logx.shortSha(sha)} -> ${Logx.shortSha(fresh.sha256)}")
                            Result.success()
                        }
                    }
                }
            }

            is UploadOutcome.Permanent -> {
                repo.markPermanent(sha, attempt, outcome.detail)
                Result.failure()
            }

            is UploadOutcome.Retry -> {
                // A ceiling, because WorkManager's own retry count is
                // effectively unbounded: a row failing for a durable reason
                // that merely LOOKS like a network error would otherwise
                // retry every five hours for ever, visible only through the
                // 24-hour silence alert — which cancels itself as soon as any
                // other upload succeeds.
                if (attempt >= MAX_ATTEMPTS) {
                    repo.markPermanent(
                        sha,
                        attempt,
                        "Gave up after $attempt attempts: ${outcome.detail}",
                    )
                    Result.failure()
                } else {
                    repo.markRetry(sha, attempt, outcome.detail)
                    Result.retry()
                }
            }
        }
    }

    companion object {
        const val KEY_SHA = "sha256"

        /**
         * 30 s base, exponential, capped by WorkManager at 5 h: ten attempts
         * span roughly a day and a half of real retrying. Past that the row is
         * parked as permanent, stays visible in the Queue screen with the
         * server's own words, and "Retry all" restarts it.
         */
        const val MAX_ATTEMPTS = 10
    }
}
