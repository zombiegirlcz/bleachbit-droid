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
 * globs `<app_cache>/**` and `<app_files>/cache/**`, so a log kept there would
 * be deleted by the very run it is recording.
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

    fun write(json: String) {
        val target = file ?: return
        try {
            target.appendBytes((json + "\n").toByteArray())
        } catch (t: Throwable) {
            Log.w(TAG, "cannot append to audit log", t)
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
