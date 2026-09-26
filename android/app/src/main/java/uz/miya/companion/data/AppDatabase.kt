package uz.miya.companion.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [UploadEntity::class, PhoneEventEntity::class],
    version = 2,
    exportSchema = false,
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun uploads(): UploadDao
    abstract fun phoneEvents(): PhoneEventDao

    companion object {

        /**
         * v1 → v2 (build step 6): the phone-event queue. A real migration and
         * NEVER fallbackToDestructiveMigration — the uploads table is the
         * record of what has already been sent, and wiping it re-uploads the
         * owner's entire call history on the next sweep.
         *
         * The SQL must match what Room generates for [PhoneEventEntity]
         * exactly (column order, types, NOT NULL, primary key, index name):
         * Room validates the schema at open and crashes on any mismatch.
         */
        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `phone_events` (" +
                        "`key` TEXT NOT NULL, " +
                        "`kind` TEXT NOT NULL, " +
                        "`payloadJson` TEXT NOT NULL, " +
                        "`state` TEXT NOT NULL, " +
                        "`attempts` INTEGER NOT NULL, " +
                        "`createdAt` INTEGER NOT NULL, " +
                        "PRIMARY KEY(`key`))"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_phone_events_state_kind_createdAt` " +
                        "ON `phone_events` (`state`, `kind`, `createdAt`)"
                )
            }
        }

        fun open(context: Context): AppDatabase =
            Room.databaseBuilder(
                context.applicationContext,
                AppDatabase::class.java,
                "miya-queue.db",
            ).addMigrations(MIGRATION_1_2).build()
    }
}
