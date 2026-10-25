package cz.zombie.bleachbitdroid

import android.content.pm.PackageManager
import android.util.Log
import org.json.JSONObject
import rikka.shizuku.Shizuku

/**
 * Executes shell commands as the `shell` user (uid 2000) through Shizuku.
 *
 * Shizuku runs the command with ADB-level privileges, which is exactly the
 * "ADB (Shizuku)" tier from AGENTS.md: more than the app's own uid, less than
 * root.
 *
 * `Shizuku.newProcess` is not part of the public Kotlin surface in API 13.x
 * (it is hidden with @RestrictTo), so we call it reflectively. If Shizuku ever
 * changes that method away, this is the single place to fix.
 */
class ShizukuRunner : ShellRunner {

    override fun run(command: String): String {
        val result = JSONObject()
        return try {
            val process = newProcess(arrayOf("sh", "-c", command))
            val stdout = process.inputStream.bufferedReader().use { it.readText() }
            val stderr = process.errorStream.bufferedReader().use { it.readText() }
            val code = process.waitFor()
            result.put("code", code)
            result.put("stdout", stdout)
            result.put("stderr", stderr)
            result.toString()
        } catch (t: Throwable) {
            Log.w(TAG, "shizuku exec failed: $command", t)
            result.put("code", -1)
            result.put("stdout", "")
            result.put("stderr", t.message ?: t.javaClass.simpleName)
            result.toString()
        }
    }

    private fun newProcess(cmd: Array<String>): Process {
        val method = Shizuku::class.java.getDeclaredMethod(
            "newProcess",
            Array<String>::class.java,
            Array<String>::class.java,
            String::class.java,
        )
        method.isAccessible = true
        return method.invoke(null, cmd, null, null) as Process
    }

    companion object {
        private const val TAG = "ShizukuRunner"
        const val REQUEST_CODE = 421

        /** True when the Shizuku service is running and reachable. */
        fun isAvailable(): Boolean = try {
            Shizuku.pingBinder()
        } catch (t: Throwable) {
            false
        }

        /** True when we already hold the Shizuku permission. */
        fun hasPermission(): Boolean = try {
            isAvailable() &&
                !Shizuku.isPreV11() &&
                Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
        } catch (t: Throwable) {
            false
        }

        /** Ask the user to grant Shizuku access; result arrives in the activity. */
        fun requestPermission() {
            if (!isAvailable()) return
            try {
                if (!hasPermission()) {
                    Shizuku.requestPermission(REQUEST_CODE)
                }
            } catch (t: Throwable) {
                Log.w(TAG, "shizuku permission request failed", t)
            }
        }
    }
}
