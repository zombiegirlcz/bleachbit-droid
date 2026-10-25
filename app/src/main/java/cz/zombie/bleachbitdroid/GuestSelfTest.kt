package cz.zombie.bleachbitdroid

import android.content.Context
import android.util.Log

/**
 * One-shot smoke test of the bundled glibc tools.
 *
 * This exists because the loader either works or it does not, and the failure
 * mode is silent: the process exits non-zero and every cleaner would simply
 * report zero bytes. Running the three tools once at startup puts the answer in
 * logcat instead of leaving it to guesswork.
 */
object GuestSelfTest {

    private const val TAG = "GuestSelfTest"

    /** Blocking; call from a background thread. */
    fun run(context: Context): Boolean {
        if (!GuestTool.isAvailable(context)) {
            Log.w(TAG, "guest tools unavailable (loader or rootfs missing)")
            return false
        }

        val shred = GuestTool.run(context, GuestTool.Tool.SHRED, listOf("--version"))
        Log.i(TAG, "shred rc=" + shred.exitCode + " out=" + shred.stdout.lineSequence().firstOrNull().orEmpty())

        val sqlite = GuestTool.sqlite(context, ":memory:", "select 1+1;")
        Log.i(TAG, "sqlite rc=" + sqlite.exitCode + " out=" + sqlite.stdout.trim())

        val find = GuestTool.run(context, GuestTool.Tool.FIND, listOf("--version"))
        Log.i(TAG, "find rc=" + find.exitCode + " out=" + find.stdout.lineSequence().firstOrNull().orEmpty())

        val ok = shred.ok && sqlite.ok && find.ok && sqlite.stdout.trim() == "2"
        Log.i(TAG, "result=" + (if (ok) "OK" else "FAILED"))
        return ok
    }
}
