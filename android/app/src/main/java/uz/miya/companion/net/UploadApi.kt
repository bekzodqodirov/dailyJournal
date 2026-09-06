package uz.miya.companion.net

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
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

        try {
            client.newCall(request).execute().use { r -> classify(r) }
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

        /** Cheap sanity check for the Settings screen. */
        fun looksLikeUrl(value: String): Boolean {
            val v = value.trim()
            return (v.startsWith("http://") || v.startsWith("https://")) && v.length > 10
        }
    }
}
