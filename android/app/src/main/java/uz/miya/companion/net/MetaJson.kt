package uz.miya.companion.net

import org.json.JSONObject
import uz.miya.companion.BuildConfig
import uz.miya.companion.data.UploadEntity
import uz.miya.companion.util.TimeFmt

/**
 * The §2.5 metadata object. This is THE contract — a second team is building
 * the server against the same document, so field names and shapes are literal.
 *
 * Required: schema, device_id, sha256, size_bytes, started_at.
 * Everything else is nullable, and the server must never 4xx because a contact
 * name was missing. JSONObject.put(key, null) DELETES the key, so every
 * optional value goes through [putOrNull], which writes JSONObject.NULL.
 */
object MetaJson {

    const val SCHEMA = 1

    fun build(row: UploadEntity, deviceId: String): JSONObject {
        val o = JSONObject()
        o.put("schema", SCHEMA)

        o.put("device_id", deviceId)
        o.putOrNull("call_id", row.callId)
        o.put("sha256", row.sha256)
        o.put("size_bytes", row.sizeBytes)

        o.put("started_at", row.startedAtIso)
        o.putOrNull("duration_seconds", row.durationSeconds)
        o.put("direction", row.direction)
        o.putOrNull("counterparty_name", row.counterpartyName)
        o.putOrNull("phone_e164", row.phoneE164)

        o.putOrNull("locale", row.locale)
        o.putOrNull("sim_slot", row.simSlot)
        o.putOrNull("mime", row.mime)
        o.putOrNull("original_filename", row.displayName)
        o.putOrNull("recorded_by", row.recordedBy)
        o.put("correlation", row.correlation)
        o.put("app_version", BuildConfig.VERSION_NAME)
        o.put("client_ts", TimeFmt.nowIso())
        return o
    }

    private fun JSONObject.putOrNull(key: String, value: Any?) {
        put(key, value ?: JSONObject.NULL)
    }
}
