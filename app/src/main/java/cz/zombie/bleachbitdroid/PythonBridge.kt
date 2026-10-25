package cz.zombie.bleachbitdroid

import android.content.Context
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

    private fun call(name: String, payload: String): JSONObject {
        val mod = module ?: error("PythonBridge.init() not called")
        val raw = mod.callAttr(name, payload).toString()
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
    }
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
