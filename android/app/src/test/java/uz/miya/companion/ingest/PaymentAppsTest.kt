package uz.miya.companion.ingest

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import uz.miya.companion.util.Keys
import java.time.ZoneId

class PaymentAppsTest {

    private fun fields(
        title: String? = "T",
        text: String? = null,
        bigText: String? = "B",
        whenMs: Long = 1727246400123L,
        groupSummary: Boolean = false,
        ongoing: Boolean = false,
        category: String? = null,
        lines: List<String> = emptyList(),
    ) = NotificationFields(
        title = title,
        text = text,
        bigText = bigText,
        subText = null,
        lines = lines,
        whenMs = whenMs,
        postTimeMs = whenMs,
        id = 7,
        tag = null,
        channelId = null,
        category = category,
        isGroupSummary = groupSummary,
        isOngoing = ongoing,
    )

    // The same vectors as tests/test_phone_notifications_api.py.
    @Test
    fun keyMatchesTheServerVectorInWholeSeconds() {
        assertEquals(
            "dev:ntf:uz.example.app:db723ede0309a96d",
            PaymentApps.notificationKey("dev", "uz.example.app", fields()),
        )
        assertEquals(
            PaymentApps.notificationKey("dev", "uz.example.app", fields(whenMs = 1727246400000L)),
            PaymentApps.notificationKey("dev", "uz.example.app", fields(whenMs = 1727246400999L)),
        )
    }

    @Test
    fun emptyBigTextCountsAsMissing() {
        val empty = fields(text = "x", bigText = "")
        val missing = fields(text = "x", bigText = null)
        assertEquals("x", PaymentApps.body(empty))
        assertEquals(
            "dev:ntf:uz.example.app:a00c3d48443a1edd",
            PaymentApps.notificationKey("dev", "uz.example.app", empty),
        )
        assertEquals(
            PaymentApps.notificationKey("dev", "uz.example.app", empty),
            PaymentApps.notificationKey("dev", "uz.example.app", missing),
        )
    }

    @Test
    fun sha256hex16MatchesAKnownVector() {
        // sha256("abc") = ba7816bf8f01cfea…
        assertEquals("ba7816bf8f01cfea", Keys.sha256hex16("abc"))
    }

    @Test
    fun clipNeverSplitsASurrogatePair() {
        val s = "ab😀c"
        assertEquals("ab", Keys.clipUtf16(s, 3))
        assertEquals("ab😀", Keys.clipUtf16(s, 4))
        assertEquals(s, Keys.clipUtf16(s, 100))
    }

    @Test
    fun onlyAllowedRealNotificationsAreCaptured() {
        val allow = setOf("uz.payme")
        val own = "uz.miya.companion"
        val sms = "com.google.android.apps.messaging"
        assertTrue(PaymentApps.shouldCapture("uz.payme", fields(), allow, own, sms))
        assertFalse(PaymentApps.shouldCapture("org.telegram", fields(), allow, own, sms))
        assertFalse(PaymentApps.shouldCapture(sms, fields(), allow + sms, own, sms))
        assertFalse(PaymentApps.shouldCapture(own, fields(), allow + own, own, sms))
        assertFalse(
            PaymentApps.shouldCapture("uz.payme", fields(groupSummary = true), allow, own, sms),
        )
        assertFalse(PaymentApps.shouldCapture("uz.payme", fields(ongoing = true), allow, own, sms))
        assertFalse(
            PaymentApps.shouldCapture(
                "uz.payme",
                fields(category = PaymentApps.CATEGORY_PROGRESS),
                allow,
                own,
                sms,
            ),
        )
        assertFalse(
            PaymentApps.shouldCapture(
                "uz.payme",
                fields(title = " ", text = "", bigText = null),
                allow,
                own,
                sms,
            ),
        )
    }

    @Test
    fun clippedHonoursTheServerLimits() {
        val long = "x".repeat(5000)
        val f = PaymentApps.clipped(
            fields(title = long, bigText = long, lines = List(30) { long }),
        )
        assertEquals(512, f.title!!.length)
        assertEquals(4096, f.bigText!!.length)
        assertEquals(20, f.lines.size)
        assertTrue(f.lines.all { it.length == 512 })
    }

    @Test
    fun payloadCarriesWholeSecondsWithTheTashkentOffset() {
        val json = JSONObject(
            PaymentApps.payloadMap("uz.payme", fields(), ZoneId.of("Asia/Tashkent")),
        ).toString()
        assertTrue(json, json.contains("\"when_at\":\"2024-09-25T11:40:00+05:00\""))
        assertTrue(json, json.contains("\"notification_id\":7"))
    }

    @Test
    fun paymeIsAutoTickedByLabelOnce() {
        assertTrue(PaymentApps.shouldAutoTick(" payme ", "a.b", emptySet(), emptySet()))
        assertFalse(PaymentApps.shouldAutoTick("Click", "a.c", emptySet(), emptySet()))
        assertFalse(PaymentApps.shouldAutoTick("Payme", "a.b", setOf("a.b"), emptySet()))
        assertFalse(PaymentApps.shouldAutoTick("Payme", "a.b", emptySet(), setOf("a.b")))
        assertFalse(PaymentApps.shouldAutoTick(null, "a.b", emptySet(), emptySet()))
    }
}
