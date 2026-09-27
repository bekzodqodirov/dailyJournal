# Minification is disabled for release (see build.gradle.kts). If you turn it
# on, keep libphonenumber's bundled metadata and Room's generated classes.
-keep class com.google.i18n.phonenumbers.** { *; }
-keepclassmembers class * extends androidx.room.RoomDatabase { public <init>(); }
