package uz.miya.companion.work

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class HeartbeatTest {

    private fun map(everGranted: Set<String> = emptySet()) = Heartbeat.heartbeatMap(
        deviceId = "dev",
        wanted = mapOf("sms" to true, "call_log" to true),
        granted = mapOf("call_log" to true, "sms" to false),
        everGranted = everGranted,
        queue = mapOf("events_pending" to 3),
        versionName = "1.0.7",
        versionCode = 7,
    )

    @Test
    fun mapsWantedGrantedAndEverGranted() {
        val json = JSONObject(map(setOf("sms")))
        assertEquals("dev", json.getString("device_id"))
        assertTrue(json.getJSONObject("wanted").getBoolean("sms"))
        assertFalse(json.getJSONObject("wanted").getBoolean("notifications"))
        assertFalse(json.getJSONObject("granted").getBoolean("sms"))
        assertTrue(json.getJSONObject("ever_granted").getBoolean("sms"))
        // Granted now counts as granted ever.
        assertTrue(json.getJSONObject("ever_granted").getBoolean("call_log"))
        assertEquals(3, json.getJSONObject("queue").getInt("events_pending"))
        assertEquals(7, json.getInt("version_code"))
    }

    @Test
    fun aStreamNeverGrantedStaysFalse() {
        val json = JSONObject(map())
        assertFalse(json.getJSONObject("ever_granted").getBoolean("sms"))
    }

    @Test
    fun dueOncePerHour() {
        assertTrue(Heartbeat.due(null, 1_000L))
        assertFalse(Heartbeat.due(0L, Heartbeat.EVERY_MS - 1))
        assertTrue(Heartbeat.due(0L, Heartbeat.EVERY_MS))
    }
}
