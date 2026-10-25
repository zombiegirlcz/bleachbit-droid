package cz.zombie.bleachbitdroid

import android.content.Context
import org.json.JSONObject

/**
 * Gives the Python core a way to `VACUUM` a SQLite database.
 *
 * Python calls this with a database path and gets back the same JSON shape the
 * shell runners use (`{code, stdout, stderr}`), so `_SqliteMixin` does not need
 * to know whether the vacuum ran through the bundled loader, Shizuku or `su`.
 *
 * On the non-root tier there is no shell at all: the app starts its own
 * bundled glibc `sqlite3` via [GuestTool], running as the app's uid.
 */
class VacuumRunner(private val context: Context) {

    @Suppress("unused") // called from Python via Chaquopy
    fun run(path: String): String {
        val result = JSONObject()
        if (!GuestTool.isAvailable(context)) {
            result.put("code", -1)
            result.put("stdout", "")
            result.put("stderr", "guest sqlite3 not installed")
            return result.toString()
        }

        val r = GuestTool.vacuum(context, path)
        result.put("code", r.exitCode)
        result.put("stdout", r.stdout)
        result.put("stderr", r.stderr)
        return result.toString()
    }
}
