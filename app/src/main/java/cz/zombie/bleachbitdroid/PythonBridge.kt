package cz.zombie.bleachbitdroid

import android.content.Context
import android.os.Build
import android.os.Environment
import android.util.Log
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject

/**
 * Thin Kotlin wrapper around the Python core (`bleachbit_droid.api`).
 *
 * The contract is deliberately JSON-in / JSON-out so the boundary can never
 * leak a Python exception into the UI thread: the Python side always returns a
 * JSON document, and errors arrive as `{"error": "..."}`.
 */
object PythonBridge {

    private var module: PyObject? = null

    fun init(context: Context) {
        if (module != null) return
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context.applicationContext))
        }
        module = Python.getInstance().getModule("bleachbit_droid.api")
    }

    private fun call(name: String, payload: String, extra: Any? = null): JSONObject {
        val mod = module ?: error("PythonBridge.init() not called")
        // The progress callback must be a separate argument: putting a Kotlin
        // object into the JSON payload makes JSONObject serialise it into the
        // string "cz.zombie.bleachbitdroid.ProgressSink@1a2b3c", so Python
        // never received a live object and the progress panel stayed empty.
        val raw = if (extra == null) {
            mod.callAttr(name, payload)
        } else {
            mod.callAttr(name, payload, extra)
        }.toString()
        // Full payload goes to logcat: the UI only shows `error`, but the
        // Python side also returns a `traceback` field we need for debugging.
        Log.d(TAG, "$name <- $payload")
        Log.d(TAG, "$name -> $raw")
        return JSONObject(raw)
    }

    private const val TAG = "PythonBridge"

    /**
     * Register the Shizuku-backed shell runner with the Python side.
     * Must be called before any request with `mode = "adb"`.
     */
    fun registerAdbRunner(runner: ShellRunner) {
        val mod = module ?: error("PythonBridge.init() not called")
        mod.callAttr("set_adb_runner", runner)
    }

    /**
     * Register the `su`-backed shell runner with the Python side.
     * Must be called before any request with `mode = "root"`.
     */
    fun registerRootRunner(runner: ShellRunner) {
        val mod = module ?: error("PythonBridge.init() not called")
        mod.callAttr("set_root_runner", runner)
    }

    /**
     * Register the bundled-`sqlite3` VACUUM bridge with the Python side.
     * Independent of the shell runners: the non-root tier has no shell.
     */
    fun registerVacuumRunner(runner: VacuumRunner) {
        val mod = module ?: error("PythonBridge.init() not called")
        mod.callAttr("set_vacuum_runner", runner)
    }

    fun detect(context: Context, mode: String): JSONObject =
        call("detect", basePayload(context, mode).toString())

    fun listCleaners(context: Context, mode: String): JSONObject =
        call("list_cleaners", basePayload(context, mode).toString())

    fun scan(
        context: Context,
        mode: String,
        cleaners: List<String>,
        unusedPackages: List<String>? = null,
    ): JSONObject = call("scan", basePayload(context, mode).apply {
        put("cleaners", JSONArray(cleaners))
        if (unusedPackages != null) put("unused_packages", JSONArray(unusedPackages))
    }.toString())

    fun scanStreaming(
        context: Context,
        mode: String,
        cleaners: List<String>,
        unusedPackages: List<String>? = null,
        sink: ProgressSink,
    ): JSONObject = call("scan_streaming", basePayload(context, mode).apply {
        put("cleaners", JSONArray(cleaners))
        if (unusedPackages != null) put("unused_packages", JSONArray(unusedPackages))
    }.toString(), sink)

    fun applyCleanStreaming(
        context: Context,
        mode: String,
        cleaners: List<String>,
        dryRun: Boolean,
        unusedPackages: List<String>? = null,
        sink: ProgressSink,
    ): JSONObject = call("apply_clean_streaming", basePayload(context, mode).apply {
        put("cleaners", JSONArray(cleaners))
        put("dry_run", dryRun)
        if (unusedPackages != null) put("unused_packages", JSONArray(unusedPackages))
    }.toString(), sink)

    fun applyClean(
        context: Context,
        mode: String,
        cleaners: List<String>,
        dryRun: Boolean,
        unusedPackages: List<String>? = null,
    ): JSONObject = call("apply_clean", basePayload(context, mode).apply {
        put("cleaners", JSONArray(cleaners))
        put("dry_run", dryRun)
        if (unusedPackages != null) put("unused_packages", JSONArray(unusedPackages))
    }.toString())

    private fun basePayload(context: Context, mode: String): JSONObject = JSONObject().apply {
        put("mode", mode)
        put("app_files", context.filesDir.absolutePath)
        put("app_cache", context.cacheDir.absolutePath)
        put("external", context.getExternalFilesDir(null)?.absolutePath ?: "")
        put("sdcard", "/sdcard")
        put("android_data", "/data/data")
        put("android_media", "/data/media/0")
        // Decides whether the non-root tier may claim `shared_storage` at all.
        // Without the grant the process can enumerate /sdcard but not delete
        // in it, so every shared-storage cleaner used to scan fine and then
        // fail on every single file.
        put("all_files", hasAllFilesAccess())
    }
}

/**
 * True when the app holds MANAGE_EXTERNAL_STORAGE (or predates the need).
 *
 * Lives next to the bridge because the Python side has to be told on every
 * call: the user can flip this in system settings while the app is running.
 */
fun hasAllFilesAccess(): Boolean =
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
        Environment.isExternalStorageManager()
    } else {
        true
    }

/**
 * A single shell command runner. Implemented by [ShizukuRunner] and
 * [RootRunner]; kept as an interface so tests and the non-root mode can stub
 * it out.
 */
interface ShellRunner {
    /** Runs `command` and returns a JSON object string `{code,stdout,stderr}`. */
    fun run(command: String): String
}
