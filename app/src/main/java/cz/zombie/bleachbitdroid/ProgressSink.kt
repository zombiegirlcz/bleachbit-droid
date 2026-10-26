package cz.zombie.bleachbitdroid

import android.util.Log
import org.json.JSONObject

/**
 * Receives one progress event per step from the Python core.
 *
 * Python calls [onEvent] with a JSON string. The name and the single-String
 * signature are dictated by `api._as_callback`, which looks up `onEvent` by
 * name and passes JSON text.
 *
 * Events are pushed to [onLine] on the calling (worker) thread; the ViewModel
 * is responsible for hopping to the main thread before touching UI state.
 */
class ProgressSink(
    private val onLine: (ProgressEvent) -> Unit,
) {

    @Suppress("unused") // called from Python via Chaquopy
    fun onEvent(json: String) {
        try {
            val o = JSONObject(json)
            onLine(
                ProgressEvent(
                    type = o.optString("type", "?"),
                    cleaner = o.optString("cleaner", ""),
                    path = o.optString("path", ""),
                    bytes = o.optLong("bytes", 0L),
                    totalBytes = o.optLong("total_bytes", 0L),
                    entries = o.optInt("entries", 0),
                    index = o.optInt("index", 0),
                    total = o.optInt("total", 0),
                    reason = o.optString("reason", ""),
                    description = o.optString("description", ""),
                )
            )
        } catch (t: Throwable) {
            Log.w(TAG, "bad progress event: $json", t)
        }
    }

    private companion object {
        const val TAG = "ProgressSink"
    }
}

/** One decoded progress event. */
data class ProgressEvent(
    val type: String,
    val cleaner: String = "",
    val path: String = "",
    val bytes: Long = 0L,
    val totalBytes: Long = 0L,
    val entries: Int = 0,
    val index: Int = 0,
    val total: Int = 0,
    val reason: String = "",
    val description: String = "",
) {
    /** Human readable Czech line, the way BleachBit shows its log. */
    fun render(): String = when (type) {
        "scan_start" -> "Prohledávám…"
        "cleaner_start" -> "• ${description.ifEmpty { cleaner }}"
        "cleaner_done" -> "  → ${cleaner}: ${formatBytes(totalBytes)} (${entries}×)"
        "scan_done" -> "Hotovo: ${formatBytes(totalBytes)} v ${entries} položkách"
        "start" -> "Spouštím čištění…"
        "scanned" -> "Nalezeno ${entries} položek (${formatBytes(totalBytes)})"
        "found" -> "  + ${shortPath(path)} (${formatBytes(bytes)})"
        "would_delete" -> "  ~ ${shortPath(path)} (${formatBytes(bytes)})"
        "deleted" -> "  − ${shortPath(path)} (${formatBytes(bytes)})"
        "skip" -> "  ! přeskočeno ${shortPath(path)}: $reason"
        "failed" -> "  ✗ ${shortPath(path)}: $reason"
        "done" -> "Uvolněno ${formatBytes(totalBytes)}"
        else -> "$type ${shortPath(path)}".trim()
    }

    private fun shortPath(path: String): String =
        if (path.length <= 60) path else "…" + path.takeLast(59)
}