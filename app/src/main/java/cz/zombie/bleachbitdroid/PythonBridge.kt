package cz.zombie.bleachbitdroid

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
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
        return JSONObject(raw)
    }

    /**
     * Register the Shizuku-backed shell runner with the Python side.
     * Must be called before any request with `mode = "adb"`.
     */
    fun registerAdbRunner(runner: ShellRunner) {
        val mod = module ?: error("PythonBridge.init() not called")
        mod.callAttr("set_adb_runner", runner)
    }

    fun detect(context: Context, mode: String): JSONObject =
        call("detect", basePayload(context, mode))

    fun listCleaners(context: Context, mode: String): JSONObject =
        call("list_cleaners", basePayload(context, mode))

    fun scan(context: Context, mode: String, cleaners: List<String>): JSONObject =
        call("scan", basePayload(context, mode).apply {
            put("cleaners", cleaners)
        })

    fun applyClean(
        context: Context,
        mode: String,
        cleaners: List<String>,
        dryRun: Boolean,
    ): JSONObject = call("apply_clean", basePayload(context, mode).apply {
        put("cleaners", cleaners)
        put("dry_run", dryRun)
    })

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
 * A single shell command runner. Implemented by [ShizukuRunner]; kept as an
 * interface so tests and the non-root mode can stub it out.
 */
interface ShellRunner {
    /** Runs `command` through `sh -c` and returns a JSON object string. */
    fun run(command: String): String
}
