package uz.miya.companion.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import uz.miya.companion.util.Logx
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The bearer token, encrypted with an AES-256-GCM key that lives in the Android
 * KeyStore and never leaves it. The ciphertext sits in a normal SharedPreferences
 * file; without the hardware-backed key it is inert.
 *
 * SharedPreferences rather than DataStore deliberately: the OkHttp auth
 * interceptor needs the token synchronously on an arbitrary thread, and a
 * blocking read from a coroutine-only store there is how you deadlock a worker.
 *
 * setUserAuthenticationRequired(false) — the phone must be able to upload while
 * locked in the owner's pocket, which is the entire point.
 */
class TokenStore(context: Context) {

    private val prefs = context.applicationContext
        .getSharedPreferences("miya_secret", Context.MODE_PRIVATE)

    fun hasToken(): Boolean = prefs.contains(KEY_CIPHERTEXT)

    /**
     * @return true only if the token is now stored. Key generation can fail —
     * an invalidated KeyStore key, a broken vendor keymaster — and the caller
     * MUST NOT report "Saved." on a false: the Health row would immediately
     * say "No bearer token saved" and the two would contradict each other with
     * no explanation of which is true.
     */
    fun save(token: String): Boolean {
        if (token.isBlank()) {
            clear()
            return false
        }
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, secretKey())
            val iv = cipher.iv
            val ct = cipher.doFinal(token.toByteArray(Charsets.UTF_8))
            prefs.edit()
                .putString(KEY_IV, Base64.encodeToString(iv, Base64.NO_WRAP))
                .putString(KEY_CIPHERTEXT, Base64.encodeToString(ct, Base64.NO_WRAP))
                .apply()
            true
        } catch (t: Throwable) {
            // Never log the token, not even on failure.
            Logx.e("Failed to seal bearer token", t)
            false
        }
    }

    fun load(): String? {
        val ivB64 = prefs.getString(KEY_IV, null) ?: return null
        val ctB64 = prefs.getString(KEY_CIPHERTEXT, null) ?: return null
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(
                Cipher.DECRYPT_MODE,
                secretKey(),
                GCMParameterSpec(TAG_BITS, Base64.decode(ivB64, Base64.NO_WRAP)),
            )
            String(cipher.doFinal(Base64.decode(ctB64, Base64.NO_WRAP)), Charsets.UTF_8)
        } catch (t: Throwable) {
            // Typically means the KeyStore key was invalidated (factory reset,
            // lock-screen change on some OEMs). Treat as "no token".
            Logx.e("Failed to open bearer token; re-enter it in Settings", t)
            null
        }
    }

    /** Masked form for the UI. The real value never reaches Compose state. */
    fun masked(): String {
        val t = load() ?: return ""
        return if (t.length <= 6) "••••••" else t.take(3) + "•".repeat(8) + t.takeLast(3)
    }

    fun clear() {
        prefs.edit().remove(KEY_IV).remove(KEY_CIPHERTEXT).apply()
    }

    private fun secretKey(): SecretKey {
        val ks = KeyStore.getInstance(PROVIDER).apply { load(null) }
        (ks.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, PROVIDER)
        generator.init(
            KeyGenParameterSpec.Builder(
                ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setUserAuthenticationRequired(false)
                .build()
        )
        return generator.generateKey()
    }

    private companion object {
        const val PROVIDER = "AndroidKeyStore"
        const val ALIAS = "miya_token_key_v1"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val TAG_BITS = 128
        const val KEY_IV = "token_iv"
        const val KEY_CIPHERTEXT = "token_ct"
    }
}
