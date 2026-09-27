package uz.miya.companion.work

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class SmsSelectionTest {

    @Test
    fun firstRunCarriesTheFrozenDateClause() {
        val from = SmsSelection.importFrom(1_700_000_000_000L, 30)
        val (where, args) = SmsSelection.smsSelection(0L, from)
        assertEquals("_id > ? AND date >= ?", where)
        assertArrayEquals(arrayOf("0", (1_700_000_000_000L - 30 * 86_400_000L).toString()), args)
    }

    @Test
    fun twoFirstRunsWithTheSamePrefGiveTheSameCutoff() {
        val frozen = 1_650_000_000_000L
        val a = SmsSelection.smsSelection(0L, frozen)
        val b = SmsSelection.smsSelection(0L, frozen)
        assertEquals(a.first, b.first)
        assertArrayEquals(a.second, b.second)
    }

    @Test
    fun afterTheFirstHarvestOnlyTheIdDecides() {
        val (where, args) = SmsSelection.smsSelection(42L, 1_650_000_000_000L)
        assertEquals("_id > ?", where)
        assertArrayEquals(arrayOf("42"), args)
    }

    @Test
    fun zeroDaysStartsFromNow() {
        assertEquals(1_000L, SmsSelection.importFrom(1_000L, 0))
    }
}
