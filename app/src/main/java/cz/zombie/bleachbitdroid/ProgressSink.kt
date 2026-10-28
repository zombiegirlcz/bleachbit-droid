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
    private val audit: CleanAudit? = null,
) {

    @Suppress("unused") // called from Python via Chaquopy
    fun onEvent(json: String) {
        // Written before parsing: a malformed event is exactly the kind of
        // thing worth having on disk afterwards.
        audit?.write(json)
        try {
            val o = JSONObject(json)
            val type = o.optString("type", "?")
            if (type == "failed" || type == "skip") {
                // The panel shortens the path and scrolls away; a failure the
                // user cannot quote is a failure nobody can fix.
                Log.w(
                    TAG,
                    "$type cleaner=${o.optString("cleaner")} " +
                        "path=${o.optString("path")} reason=${o.optString("reason")}",
                )
            }
            onLine(
                ProgressEvent(
                    type = type,
                    cleaner = o.optString("cleaner", ""),
                    path = o.optString("path", ""),
                    bytes = o.optLong("bytes", 0L),
                    totalBytes = o.optLong("total_bytes", 0L),
                    // `done` reports what the clean actually freed, and the
                    // package tiers report whether that number is even
                    // knowable. Both were missing here, so the last line of
                    // every clean read "Uvolněno 0 B".
                    freedBytes = o.optLong("freed_bytes", 0L),
                    sizeKnown = o.optBoolean("size_known", true),
                    unknown = o.optInt("unknown", 0),
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
    val freedBytes: Long = 0L,
    val sizeKnown: Boolean = true,
    val unknown: Int = 0,
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
        // `bytes` here is this cleaner's own share, not the running total.
        "cleaner_done" -> "  → ${cleaner}: ${formatBytes(bytes)} (${entries}×)"
        "scan_done" -> "Hotovo: ${formatBytes(totalBytes)} v ${entries} položkách"
        "start" -> "Spouštím čištění…"
        "scanned" -> "Nalezeno ${entries} položek (${formatBytes(bytes)})"
        "found" -> "  + ${shortPath(path)} (${formatBytes(bytes)})"
        "would_delete" -> "  ~ ${shortPath(path)} (${formatBytes(bytes)})"
        "deleted" ->
            if (sizeKnown) "  − ${shortPath(path)} (${formatBytes(bytes)})"
            else "  − ${shortPath(path)} (velikost neznámá)"
        "skip" -> "  ! přeskočeno ${shortPath(path)}: $reason"
        "failed" -> "  ✗ ${shortPath(path)}: $reason"
        "done" ->
            if (unknown > 0) {
                "Uvolněno ${formatBytes(freedBytes)} + $unknown položek neznámé velikosti"
            } else {
                "Uvolněno ${formatBytes(freedBytes)}"
            }
        else -> "$type ${shortPath(path)}".trim()
    }

    private fun shortPath(path: String): String =
        if (path.length <= 60) path else "…" + path.takeLast(59)
}