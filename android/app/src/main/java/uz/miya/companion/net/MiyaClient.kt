package uz.miya.companion.net

import okhttp3.OkHttpClient
import okhttp3.Response
import uz.miya.companion.data.TokenStore
import java.util.concurrent.TimeUnit

/**
 * One OkHttpClient for the whole process.
 *
 * Timeouts are deliberately asymmetric: 30 s to connect and 60 s to read a
 * response, but NO call timeout and NO write timeout, because a 40 MB upload
 * over a 3G link in Tashkent legitimately takes minutes and a call timeout
 * would kill it mid-transfer for no reason.
 */
object MiyaClient {

    fun build(tokenStore: TokenStore): OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(0, TimeUnit.MILLISECONDS)
            .callTimeout(0, TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(true)
            .addInterceptor { chain ->
                // Read the token per request: it can be changed in Settings
                // while a worker is mid-backoff, and the next attempt should
                // pick that up without a process restart.
                val token = tokenStore.load()
                val request = if (token.isNullOrBlank()) {
                    chain.request()
                } else {
                    chain.request().newBuilder()
                        .header("Authorization", "Bearer $token")
                        .build()
                }
                chain.proceed(request)
            }
            .build()

    /** Body text, bounded, for error reporting. Never logs the request. */
    fun safeBody(response: Response, limit: Long = 4096): String = try {
        response.peekBody(limit).string()
    } catch (t: Throwable) {
        ""
    }
}
