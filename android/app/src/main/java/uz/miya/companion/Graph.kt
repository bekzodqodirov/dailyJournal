package uz.miya.companion

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import okhttp3.OkHttpClient
import uz.miya.companion.data.AppDatabase
import uz.miya.companion.data.Prefs
import uz.miya.companion.data.RecordingRepository
import uz.miya.companion.data.TokenStore
import uz.miya.companion.ingest.RecordingScanner
import uz.miya.companion.net.MiyaClient
import uz.miya.companion.net.UploadApi
import uz.miya.companion.watch.WatchArmer

/**
 * Hand-rolled service locator. At this size a DI framework would be more code
 * than it saves, and workers and broadcast receivers can be re-entered after
 * process death — so [init] is idempotent and everything is lazy.
 */
object Graph {

    @Volatile
    private var initialised = false

    lateinit var appContext: Context
        private set

    val appScope: CoroutineScope by lazy {
        CoroutineScope(SupervisorJob() + Dispatchers.Default)
    }

    val database: AppDatabase by lazy { AppDatabase.open(appContext) }
    val prefs: Prefs by lazy { Prefs(appContext) }
    val tokenStore: TokenStore by lazy { TokenStore(appContext) }
    val http: OkHttpClient by lazy { MiyaClient.build(tokenStore) }
    val api: UploadApi by lazy { UploadApi(appContext, http) }
    val repository: RecordingRepository by lazy {
        RecordingRepository(appContext, database.uploads(), prefs)
    }
    val scanner: RecordingScanner by lazy { RecordingScanner(appContext, prefs, repository) }

    /**
     * Held here rather than in a local, because a garbage-collected
     * FileObserver silently stops delivering events.
     */
    val watchArmer: WatchArmer by lazy { WatchArmer(appContext, prefs, appScope) }

    @Synchronized
    fun init(context: Context) {
        if (initialised) return
        appContext = context.applicationContext
        initialised = true
    }
}
