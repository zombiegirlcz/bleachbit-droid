package cz.zombie.bleachbitdroid

import android.util.Log
import org.json.JSONObject
import java.io.DataOutputStream

/**
 * Executes shell commands as real root via `su`.
 *
 * The Python ``RootBackend`` expects a runner whose ``run()`` takes the raw
 * command and returns JSON `{code, stdout, stderr}`. ``RootBackend`` itself
 * wraps the command in `su -c '...'`, so this runner runs whatever it is given
 * through a root shell.
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
            val code = process.waitFor()
            result.put("code", code)
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

        /** True when `su` exists and actually grants uid 0. Blocking call. */
        fun isAvailable(): Boolean = try {
            val process = Runtime.getRuntime().exec("su")
            DataOutputStream(process.outputStream).use { stdin ->
                stdin.writeBytes("id\n")
                stdin.writeBytes("exit\n")
                stdin.flush()
            }
            val out = process.inputStream.bufferedReader().use { it.readText() }
            process.waitFor()
            out.contains("uid=0")
        } catch (t: Throwable) {
            false
        }
    }
}
