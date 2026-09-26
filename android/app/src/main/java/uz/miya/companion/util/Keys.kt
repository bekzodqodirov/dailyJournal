package uz.miya.companion.util

import java.security.MessageDigest

/**
 * The hashing and clipping the server mirrors byte for byte. Pure JVM: no
 * android.* here, so plain unit tests can pin the vectors.
 */
object Keys {
    private const val HEX_DIGITS = "0123456789abcdef"

    /**
     * The first 8 bytes of SHA-256 as lower-case hex = Python's
     * hexdigest()[:16]. Built by hand so no locale can ever localise a
     * "digit" of a key.
     */
    fun sha256hex16(s: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(s.toByteArray(Charsets.UTF_8))
        val hex = StringBuilder(16)
        for (i in 0 until 8) {
            val b = digest[i].toInt() and 0xff
            hex.append(HEX_DIGITS[b ushr 4]).append(HEX_DIGITS[b and 0x0f])
        }
        return hex.toString()
    }

    /**
     * take() counts UTF-16 units; never cut an emoji's surrogate pair in
     * half — org.json would emit malformed UTF-8 the server may refuse.
     */
    fun clipUtf16(s: String, max: Int): String {
        var clipped = s.take(max)
        if (clipped.isNotEmpty() && clipped.last().isHighSurrogate()) {
            clipped = clipped.dropLast(1)
        }
        return clipped
    }
}
