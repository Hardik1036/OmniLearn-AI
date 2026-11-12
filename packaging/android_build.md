# OmniLearn AI — Android `.apk` Build Guide (Capacitor + WebView)

The OmniLearn AI Android application wraps the Pure CSS3 + Google Lit Web App (`frontend/`) inside a native Android WebView via **Capacitor** and connects directly to either:
1. Your hosted **Cloudflare Worker** (`https://omnilearn-ai.<subdomain>.workers.dev`), or
2. Your desktop **System App** running in `--headless` mode over your local network (`http://<desktop-lan-ip>:<port>/?local_token=...`).

---

## 1. Prerequisites

- **Node.js 20+** and `npm`
- **Java JDK 17** (`JAVA_HOME` configured)
- **Android Studio / Android SDK** (API Level 34+ & Build Tools installed, `ANDROID_HOME` configured)

---

## 2. Initialize Capacitor Android Project

Run these commands inside a separate `packaging/android-workspace/` staging directory (or copy `packaging/capacitor.config.json` to your Capacitor build folder so root repository config stays clean):

```bash
mkdir -p build/capacitor-android && cd build/capacitor-android
npm init -y
npm install @capacitor/core @capacitor/cli @capacitor/android
cp ../../packaging/capacitor.config.json ./capacitor.config.json
# Adjust webDir path in the copied config so it resolves to ../../frontend
node -e "const fs=require('fs'),c=JSON.parse(fs.readFileSync('capacitor.config.json'));c.webDir='../../frontend';fs.writeFileSync('capacitor.config.json',JSON.stringify(c,null,2));"
npx cap add android
```

---

## 3. Configure Endpoint Switching & Sync Static Assets

Sync the frontend (`frontend/`) into the Android native asset bundle whenever `frontend/` changes:

```bash
npx cap sync android
```

> **Note:** In `frontend/js/api-client.js`, when running on Capacitor WebView (`window.Capacitor`), configure the target Cloudflare Worker URL (or LAN System App endpoint) via `localStorage.setItem('omnilearn_api_base', 'https://omnilearn-ai.<your-subdomain>.workers.dev')` or via the in-app **Admin Login / Cloud Sync** settings modal.

---

## 4. Build Debug or Signed Release `.apk`

### Debug APK (Sideload Testing)

```bash
cd android
./gradlew assembleDebug
```

Output path:
`android/app/build/outputs/apk/debug/app-debug.apk`

### Signed Release APK

1. Generate a release keystore (one-time):

```bash
keytool -genkey -v -keystore omnilearn-release.jks -keyalg RSA -keysize 2048 -validity 10000 -alias omnilearn
```

2. Export signing properties (do **not** commit keystore passwords to Git):

```bash
export ANDROID_KEYSTORE_PATH="$(pwd)/omnilearn-release.jks"
export ANDROID_KEY_ALIAS="omnilearn"
```

3. Build the release APK:

```bash
cd android
./gradlew assembleRelease
```

Sign/align (if not using `signingConfigs` in `app/build.gradle`) and verify with `apksigner`:

```bash
apksigner sign --ks ../omnilearn-release.jks --out OmniLearn-v1.0.0.apk app/build/outputs/apk/release/app-release-unsigned.apk
apksigner verify OmniLearn-v1.0.0.apk
```

---

## 5. Generate SHA-256 Checksum & Upload for Download Center

Compute the checksum published alongside GitHub Releases / Cloudflare R2 (`download-modal.js`):

```bash
sha256sum OmniLearn-v1.0.0.apk > OmniLearn-v1.0.0.apk.sha256
```

On Windows PowerShell:

```powershell
(Get-FileHash .\OmniLearn-v1.0.0.apk -Algorithm SHA256).Hash.ToLower()
```

Verify installation on a USB-connected Android device or emulator:

```bash
adb install -r OmniLearn-v1.0.0.apk
```
