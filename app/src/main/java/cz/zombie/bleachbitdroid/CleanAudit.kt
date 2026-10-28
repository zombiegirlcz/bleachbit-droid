package cz.zombie.bleachbitdroid

import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Appends one JSON line per progress event to a file under `filesDir/logs`.
 *
 * Why this exists: the app is a destructive tool that kept no record of what
 * it deleted. When a clean went wrong on the test device the only way to find
 * out what had happened was to reconstruct it from the *system* log — which
 * does not contain the app's own reasons at all, because they only ever went
 * to the on-screen panel and died with it. A failure the user can describe but
 * not quote is a failure nobody can fix.
 *
 * `filesDir/logs` is deliberately outside `cacheDir`: the `app_cache` cleaner
 * globs the whole cache directory and `files/cache` recursively, so a log kept
 * there would be deleted by the very run it is recording. (The glob patterns
 * are not spelled out here on purpose -- a slash-star inside a KDoc block
 * opens a nested Kotlin comment and broke the build once already.)
 */
class CleanAudit(filesDir: File, mode: String) {

    private val file: File? = try {
        val dir = File(filesDir, "logs").apply { mkdirs() }
        prune(dir)
        File(dir, "clean-${stamp()}-$mode.jsonl")
    } catch (t: Throwable) {
        // Never let bookkeeping break a clean.
        Log.w(TAG, "cannot open audit log", t)
        null
    }

    val path: String? get() = file?.absolutePath

    private val lock = Any()

    /**
     * Append one event.
     *
     * Synchronized because the log is evidence, and evidence that can be
     * spliced is worthless. One line in the 02:54 clean log read
     * as a `found` event for cleaner `logcat_dumps` whose path broke off at
     * `/da`, followed by the tail of a completely different event -- two
     * writers landing in one line, which also hid the breakage from a JSON
     * parser, since a duplicated key still parses. Whatever produced the
     * second writer, the log itself must not be the thing that loses the
     * proof. (No brace is quoted here on purpose: a stray one in a comment
     * makes every brace-balance check read this file as broken.)
     */
    fun write(json: String) {
        val target = file ?: return
        synchronized(lock) {
            try {
                target.appendBytes((json + "\n").toByteArray())
            } catch (t: Throwable) {
                Log.w(TAG, "cannot append to audit log", t)
            }
        }
    }

    private companion object {
        const val TAG = "CleanAudit"

        /** Keep the last few runs; this is a diagnostic, not an archive. */
        const val KEEP = 20

        fun stamp(): String =
            SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())

        fun prune(dir: File) {
            val logs = dir.listFiles { f -> f.name.endsWith(".jsonl") } ?: return
            logs.sortedByDescending { it.name }.drop(KEEP).forEach { it.delete() }
        }
    }
}
