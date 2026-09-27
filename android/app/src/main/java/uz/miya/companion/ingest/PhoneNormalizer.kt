package uz.miya.companion.ingest

import android.content.Context
import android.telephony.TelephonyManager
import com.google.i18n.phonenumbers.NumberParseException
import com.google.i18n.phonenumbers.PhoneNumberUtil
import uz.miya.companion.util.Logx
import java.util.Locale

/**
 * Number formatting is inconsistent across OEMs and even between two calls on
 * one device: +998901234567, 998901234567, 0901234567, with spaces, with
 * dashes. Everything is normalised to E.164 against the SIM's country before it
 * leaves the phone, and a last-9-digits comparison is used as the fallback —
 * which is deliberately the same rule the server's people.find_by_phone uses.
 */
object PhoneNormalizer {

    fun regionCode(context: Context): String {
        val tm = try {
            context.getSystemService(TelephonyManager::class.java)
        } catch (t: Throwable) {
            null
        }
        val sim = tm?.simCountryIso?.takeIf { it.isNotBlank() }
            ?: tm?.networkCountryIso?.takeIf { it.isNotBlank() }
            ?: Locale.getDefault().country
        return sim.uppercase(Locale.ROOT).takeIf { it.length == 2 } ?: "UZ"
    }

    /** E.164, or null if the input cannot be a phone number at all. */
    fun toE164(raw: String?, region: String): String? {
        if (raw.isNullOrBlank()) return null
        val trimmed = raw.trim()
        // Withheld numbers arrive as a localized "Unknown"/"Private", or as -1/-2.
        if (trimmed.none { it.isDigit() }) return null
        if (trimmed == "-1" || trimmed == "-2" || trimmed == "-3") return null

        return try {
            val util = PhoneNumberUtil.getInstance()
            val parsed = util.parse(trimmed, region)
            if (util.isValidNumber(parsed)) {
                util.format(parsed, PhoneNumberUtil.PhoneNumberFormat.E164)
            } else {
                fallback(trimmed)
            }
        } catch (e: NumberParseException) {
            fallback(trimmed)
        } catch (t: Throwable) {
            Logx.w("libphonenumber failed: ${t.message}")
            fallback(trimmed)
        }
    }

    /**
     * If libphonenumber will not validate it, keep the digits with a leading +
     * when it plausibly already carried a country code. Sending a slightly
     * wrong number beats sending null: the server can still fuzzy-match on the
     * last nine digits, and it never 4xxs on a bad one.
     */
    private fun fallback(raw: String): String? {
        val digits = raw.filter(Char::isDigit)
        if (digits.length < 7) return null
        return if (raw.trimStart().startsWith("+")) "+$digits" else digits
    }

    /** Last-9-digit equality, for matching a filename number to a call-log row. */
    fun looselyEqual(a: String?, b: String?): Boolean {
        val da = a?.filter(Char::isDigit) ?: return false
        val db = b?.filter(Char::isDigit) ?: return false
        if (da.isEmpty() || db.isEmpty()) return false
        val n = minOf(9, da.length, db.length)
        if (n < 6) return da == db
        return da.takeLast(n) == db.takeLast(n)
    }
}
