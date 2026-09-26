plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.devtools.ksp")
}

android {
    namespace = "uz.miya.companion"
    compileSdk = 36

    defaultConfig {
        applicationId = "uz.miya.companion"
        // 29 = Android 10. Below that the multi-directory FileObserver
        // constructor and MediaStore RELATIVE_PATH do not exist, and the
        // whole scoped-storage story is different.
        minSdk = 29
        targetSdk = 36
        // CI passes the run number, so every release installs over the last.
        val ciVersionCode = System.getenv("MIYA_VERSION_CODE")?.toIntOrNull() ?: 1
        versionCode = ciVersionCode
        versionName = "1.0.$ciVersionCode"
    }

    // Debug builds use each developer's own ~/.android/debug.keystore. The
    // release key lives only in the "release" GitHub environment (master
    // only); CI decodes it to a temp file and passes the path here. Without
    // it the release APK stays unsigned and will not install — loud, not
    // silent.
    val releaseKeystore = System.getenv("MIYA_KEYSTORE_PATH")
    signingConfigs {
        if (releaseKeystore != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = System.getenv("MIYA_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("MIYA_KEY_ALIAS")
                keyPassword = System.getenv("MIYA_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            // Keep R8 off for v1: this app is sideloaded to exactly one phone,
            // and a stripped stack trace on an OEM-specific failure costs far
            // more than the few hundred KB saved.
            isMinifyEnabled = false
            if (releaseKeystore != null) {
                signingConfig = signingConfigs.getByName("release")
            }
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")

    val composeBom = platform("androidx.compose:compose-bom:2024.10.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    debugImplementation("androidx.compose.ui:ui-tooling")

    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("androidx.datastore:datastore-preferences:1.1.1")
    implementation("androidx.documentfile:documentfile:1.0.1")

    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    implementation("com.googlecode.libphonenumber:libphonenumber:8.13.42")

    testImplementation("junit:junit:4.13.2")
    // android.jar's org.json is a stub that throws "not mocked" on the JVM;
    // the real implementation shadows it on the test classpath.
    testImplementation("org.json:json:20240303")
}
