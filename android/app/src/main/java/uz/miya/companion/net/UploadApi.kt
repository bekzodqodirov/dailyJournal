package uz.miya.companion.net

import android.content.Context
import android.security.NetworkSecurityPolicy
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import uz.miya.companion.data.RecordingRef
import uz.miya.companion.data.UploadEntity
import uz.miya.companion.util.Logx
import java.io.IOException
import java.net.UnknownServiceException

/**
 * The wire contract from §2.3, §2.4 and §2.7. Nothing else in the app knows
 * about HTTP status codes.
 */
sealed class UploadOutcome {
    /** 202. */
    object Accepted : UploadOutcome()

    /** 200 duplicate. Success, NOT an error — see §4.3. */
    object Duplicate : UploadOutcome()

    /** 401. Stop everything and shout; retrying a bad token forever is useless. */
    data class AuthFailed(val detail: String) : UploadOutcome()

    /** 422 "sha256 mismatch". Re-hash locally and retry exactly once. */
    data class HashMismatch(val detail: String) : UploadOutcome()

    /** 422 anything else (oversize, bad shape). Never retry. */
    data class Permanent(val detail: String) : UploadOutcome()

    /** 5xx / 503 / timeout / reset. Exponential backoff. */
    data class Retry(val detail: String) : UploadOutcome()
}

data class ProbeResult(
    val knownSha256: Set<String>,
    val knownCallId: Set<String>,
)

class UploadApi(
    private val context: Context,
    private val client: OkHttpClient,
) {

    private val jsonType = "application/json; charset=utf-8".toMediaTypeOrNull()

    /** Reachability only: no auth required on /health server-side. */
    suspend fun health(baseUrl: String): Result<String> = withContext(Dispatchers.IO) {
        runCatching {
            val request = Request.Builder().url(join(baseUrl, "/health")).get().build()
            client.newCall(request).execute().use { r ->
                if (!r.isSuccessful) throw IOException("HTTP ${r.code} from /health")
                MiyaClient.safeBody(r)
            }
        }
    }

    /**
     * §2.3. Cheap "do you already have this?", so the phone does not push
     * 40 MB over a slow link to re-learn that a previous attempt succeeded but
     * its response was lost. Batch limit is 200 per call, server-side.
     */
    suspend fun probe(
        baseUrl: String,
        sha256: List<String>,
        callIds: List<String>,
    ): Result<ProbeResult> = withContext(Dispatchers.IO) {
        runCatching {
            val payload = JSONObject()
                .put("sha256", JSONArray(sha256.take(200)))
                .put("call_id", JSONArray(callIds.take(200)))
            val request = Request.Builder()
                .url(join(baseUrl, "/v1/recordings/probe"))
                .post(payload.toString().toRequestBody(jsonType))
                .build()
            client.newCall(request).execute().use { r ->
                if (r.code == 401) throw AuthException(detailOf(r))
                if (!r.isSuccessful) throw IOException("HTTP ${r.code} from probe: ${detailOf(r)}")
                val body = JSONObject(MiyaClient.safeBody(r, 1 shl 20))
                ProbeResult(
                    knownSha256 = body.optJSONArray("known_sha256").toStringSet(),
                    knownCallId = body.optJSONArray("known_call_id").toStringSet(),
                )
            }
        }
    }

    /**
     * §2.4. EXACTLY two parts, `meta` first so the server can reject on
     * metadata before spooling bytes, `audio` second.
     *
     * The filename is provenance only — the server takes the suffix and
     * nothing else — but it is still sent, because it is the only record of
     * what the OEM called the file.
     */
    suspend fun upload(
        baseUrl: String,
        row: UploadEntity,
        ref: RecordingRef,
        deviceId: String,
    ): UploadOutcome = withContext(Dispatchers.IO) {
        // EVERYTHING is inside the try, including building the request.
        // HttpUrl.url(String) throws IllegalArgumentException for anything that
        // is not a valid http/https URL, so a typo like "100.90.1.5:8000" used
        // to escape doWork() entirely: WorkManager marked the work failed with
        // no retry while the Room row sat in UPLOADING with lastError = null —
        // an uploading row that never moves and a Health screen showing no
        // error at all.
        try {
            val meta = MetaJson.build(row, deviceId).toString()
            val audioType = (row.mime ?: "audio/mp4").toMediaTypeOrNull()

            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("meta", null, meta.toRequestBody(jsonType))
                .addFormDataPart(
                    "audio",
                    row.displayName,
                    StreamingRequestBody(
                        context.contentResolver,
                        ref.uri,
                        audioType,
                        row.sizeBytes,
                    ),
                )
                .build()

            val request = Request.Builder()
                .url(join(baseUrl, "/v1/recordings"))
                .post(body)
                .build()

            client.newCall(request).execute().use { r -> classify(r) }
        } catch (changed: StreamingRequestBody.SourceChangedException) {
            // The OEM rewrote or truncated the file between hashing and
            // uploading. Retrying is pointless — the declared Content-Length
            // can never be met — but the next scan will see the new size,
            // hash the file again and queue it as a fresh row.
            UploadOutcome.Permanent(
                "The recording changed on disk mid-upload (${changed.message}). " +
                    "It will be re-hashed and re-queued by the next scan."
            )
        } catch (cleartext: UnknownServiceException) {
            // Cleartext blocked by the network security policy. Retrying this
            // forever is what an IOException classification would do, and the
            // error would point at the tunnel instead of at the real cause.
            UploadOutcome.Permanent(cleartextMessage(baseUrl, cleartext))
        } catch (bad: IllegalArgumentException) {
            UploadOutcome.Permanent(
                "Bad server URL \"$baseUrl\": ${bad.message ?: "not a valid http/https URL"}. " +
                    "Fix it in Settings."
            )
        } catch (io: IOException) {
            // Timeout, connection reset, DNS failure, tunnel down.
            UploadOutcome.Retry(io.message ?: io.javaClass.simpleName)
        } catch (t: Throwable) {
            // The source file vanished mid-upload (OEM cleaner) lands here.
            UploadOutcome.Permanent(t.message ?: t.javaClass.simpleName)
        }
    }

    private fun classify(r: Response): UploadOutcome {
        val detail = detailOf(r)
        return when (r.code) {
            202 -> UploadOutcome.Accepted
            200 -> UploadOutcome.Duplicate
            401 -> UploadOutcome.AuthFailed(detail.ifBlank { "Invalid or missing bearer token" })
            422 ->
                if (detail.contains("sha256", ignoreCase = true) &&
                    detail.contains("mismatch", ignoreCase = true)
                ) {
                    UploadOutcome.HashMismatch(detail)
                } else {
                    UploadOutcome.Permanent(detail.ifBlank { "Rejected by server (422)" })
                }
            // 503 is "server booting / DB unreachable" — always retryable.
            in 500..599 -> UploadOutcome.Retry("HTTP ${r.code}: $detail")
            // 413 from a reverse proxy the design does not assume but a user
            // might add: treat as permanent, the file will never fit.
            413 -> UploadOutcome.Permanent("HTTP 413: payload too large")
            408, 429 -> UploadOutcome.Retry("HTTP ${r.code}: $detail")
            else -> {
                Logx.w("Unexpected upload status ${r.code}: $detail")
                UploadOutcome.Permanent("HTTP ${r.code}: $detail")
            }
        }
    }

    /** FastAPI puts the message in `detail`, which may be a string or a list. */
    private fun detailOf(r: Response): String {
        val raw = MiyaClient.safeBody(r)
        if (raw.isBlank()) return ""
        return try {
            val o = JSONObject(raw)
            when (val d = o.opt("detail")) {
                null -> raw.take(400)
                is String -> d
                is JSONArray -> (0 until d.length()).joinToString("; ") { i ->
                    val item = d.opt(i)
                    if (item is JSONObject) item.optString("msg", item.toString()) else item.toString()
                }
                else -> d.toString()
            }.take(400)
        } catch (t: Throwable) {
            raw.take(400)
        }
    }

    private fun JSONArray?.toStringSet(): Set<String> {
        if (this == null) return emptySet()
        val out = HashSet<String>(length())
        for (i in 0 until length()) {
            optString(i, null)?.takeIf { it.isNotBlank() }?.let { out.add(it) }
        }
        return out
    }

    class AuthException(message: String) : IOException(message)

    companion object {
        fun join(baseUrl: String, path: String): String {
            val base = baseUrl.trim().trimEnd('/')
            return base + path
        }

        /**
         * Sanity check for the Settings screen, and the same predicate the
         * ViewModel refuses to save on. It parses rather than pattern-matches,
         * because the thing that must not happen later is HttpUrl throwing
         * inside a worker.
         */
        fun looksLikeUrl(value: String): Boolean {
            val v = value.trim().trimEnd('/')
            if (!v.startsWith("http://") && !v.startsWith("https://")) return false
            val parsed = v.toHttpUrlOrNull() ?: return false
            return parsed.host.isNotBlank()
        }

        /**
         * The host, if plaintext HTTP to it is refused by the network security
         * policy in res/xml/network_security_config.xml; null when the URL is
         * https, unparseable, or permitted. This is the platform's own answer,
         * asked BEFORE the first request, so the owner is told the truth at
         * save time instead of watching an upload retry forever.
         */
        fun cleartextBlockedHost(value: String): String? {
            val parsed = value.trim().trimEnd('/').toHttpUrlOrNull() ?: return null
            if (parsed.scheme != "http") return null
            val policy = NetworkSecurityPolicy.getInstance()
            return if (policy.isCleartextTrafficPermitted(parsed.host)) null else parsed.host
        }

        /** One wording for the cleartext failure, wherever it surfaces. */
        fun cleartextAdvice(host: String): String =
            "Android blocks plaintext HTTP to $host. Use the tailnet MagicDNS name " +
                "(http://<machine>.<tailnet>.ts.net:8000), or add $host to " +
                "res/xml/network_security_config.xml and rebuild. See the README, " +
                "\"Plaintext HTTP and the network security config\"."

        private fun cleartextMessage(baseUrl: String, e: Throwable): String {
            val host = baseUrl.trim().trimEnd('/').toHttpUrlOrNull()?.host ?: baseUrl
            return cleartextAdvice(host) + " (${e.message})"
        }
    }
}
