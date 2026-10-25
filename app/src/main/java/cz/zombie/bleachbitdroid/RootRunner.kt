package cz.zombie.bleachbitdroid

import android.util.Log
import org.json.JSONObject
import java.io.DataOutputStream
import java.util.concurrent.TimeUnit

/**
 * Executes shell commands as real root via `su`.
 *
 * The Python ``RootBackend`` expects a runner whose ``run()`` takes the raw
 * command and returns JSON `{code, stdout, stderr}`. ``RootBackend`` itself
 * wraps the command in `su -c '...'`, so this runner runs whatever it is given
 * through a root shell.
 *
 * Every wait has a timeout: `su` shows a Magisk superuser prompt and blocks
 * until the user answers. Without a timeout `isAvailable()` never returns and
 * the whole startup path (permission refresh -> mode detection) hangs.
 */
class RootRunner : ShellRunner {

    override fun run(command: String): String {
        val result = JSONObject()
        return try {
            val process = Runtime.getRuntime().exec("su")
            DataOutputStream(process.outputStream).use { stdin ->
                stdin.writeBytes(command + "\n")
                stdin.writeBytes("exit\n")
                stdin.flush()
            }
            val stdout = process.inputStream.bufferedReader().use { it.readText() }
            val stderr = process.errorStream.bufferedReader().use { it.readText() }
            val finished = process.waitFor(EXEC_TIMEOUT_SEC, TimeUnit.SECONDS)
            if (!finished) {
                process.destroyForcibly()
                result.put("code", -1)
                result.put("stdout", stdout)
                result.put("stderr", "timeout waiting for su")
                return result.toString()
            }
            result.put("code", process.exitValue())
            result.put("stdout", stdout)
            result.put("stderr", stderr)
            result.toString()
        } catch (t: Throwable) {
            Log.w(TAG, "su exec failed", t)
            result.put("code", -1)
            result.put("stdout", "")
            result.put("stderr", t.message ?: t.javaClass.simpleName)
            result.toString()
        }
    }

    companion object {
        private const val TAG = "RootRunner"

        /** Short: a pending Magisk prompt must not block startup. */
        private const val PROBE_TIMEOUT_SEC = 5L
        private const val EXEC_TIMEOUT_SEC = 120L

        /**
         * True when `su` exists and grants uid 0. Blocking, but bounded — the
         * Magisk prompt would otherwise block forever.
         */
        fun isAvailable(): Boolean = try {
            val process = Runtime.getRuntime().exec("su")
            DataOutputStream(process.outputStream).use { stdin ->
                stdin.writeBytes("id\n")
                stdin.writeBytes("exit\n")
                stdin.flush()
            }
            val out = process.inputStream.bufferedReader().use { it.readText() }
            val finished = process.waitFor(PROBE_TIMEOUT_SEC, TimeUnit.SECONDS)
            if (!finished) {
                process.destroyForcibly()
                Log.i(TAG, "su probe timed out (Magisk prompt pending?)")
                false
            } else {
                out.contains("uid=0")
            }
        } catch (t: Throwable) {
            false
        }
    }
}