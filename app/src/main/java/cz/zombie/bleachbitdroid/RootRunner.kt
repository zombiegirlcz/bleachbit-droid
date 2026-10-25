package cz.zombie.bleachbitdroid

import android.util.Log
import org.json.JSONObject
import java.io.DataOutputStream
import java.io.InputStream
import java.util.concurrent.TimeUnit

/**
 * Executes shell commands as real root via `su`.
 *
 * Two things here are deliberate and were both bugs once:
 *
 *  1. Every wait has a timeout: `su` may show a Magisk prompt and block until
 *     the user answers.
 *  2. Streams are drained on background threads, never inline before the wait.
 *     `inputStream.readText()` blocks until EOF, which never comes while `su`
 *     sits on a prompt, so reading first made the timeout unreachable and hung
 *     app startup forever.
 */
class RootRunner : ShellRunner {

    override fun run(command: String): String {
        val result = JSONObject()
        var process: Process? = null
        return try {
            process = Runtime.getRuntime().exec("su")
            DataOutputStream(process.outputStream).use { stdin ->
                stdin.writeBytes(command + "\n")
                stdin.writeBytes("exit\n")
                stdin.flush()
            }

            val stdout = StringBuilder()
            val stderr = StringBuilder()
            val outThread = drain(process.inputStream, stdout)
            val errThread = drain(process.errorStream, stderr)

            val finished = process.waitFor(EXEC_TIMEOUT_SEC, TimeUnit.SECONDS)
            if (!finished) {
                process.destroyForcibly()
                outThread.join(1000)
                errThread.join(1000)
                result.put("code", -1)
                result.put("stdout", stdout.toString())
                result.put("stderr", "timeout waiting for su")
                return result.toString()
            }
            outThread.join(2000)
            errThread.join(2000)
            result.put("code", process.exitValue())
            result.put("stdout", stdout.toString())
            result.put("stderr", stderr.toString())
            result.toString()
        } catch (t: Throwable) {
            process?.destroyForcibly()
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

        private fun drain(stream: InputStream, sink: StringBuilder): Thread {
            val t = Thread {
                try {
                    stream.bufferedReader().use { reader ->
                        val buf = CharArray(4096)
                        while (true) {
                            val n = reader.read(buf)
                            if (n < 0) break
                            synchronized(sink) { sink.append(buf, 0, n) }
                        }
                    }
                } catch (_: Throwable) {
                    // process died or stream closed - nothing to add
                }
            }
            t.isDaemon = true
            t.start()
            return t
        }

        /**
         * True when `su` exists and grants uid 0. Blocking, but bounded.
         *
         * Streams are drained in the background: `readText()` here would block
         * on a Magisk prompt forever and defeat the timeout.
         */
        fun isAvailable(): Boolean {
            var process: Process? = null
            return try {
                process = Runtime.getRuntime().exec("su")
                DataOutputStream(process.outputStream).use { stdin ->
                    stdin.writeBytes("id\n")
                    stdin.writeBytes("exit\n")
                    stdin.flush()
                }
                val out = StringBuilder()
                val t = drain(process.inputStream, out)
                val finished = process.waitFor(PROBE_TIMEOUT_SEC, TimeUnit.SECONDS)
                if (!finished) {
                    process.destroyForcibly()
                    Log.i(TAG, "su probe timed out (Magisk prompt pending?)")
                    false
                } else {
                    t.join(1000)
                    val text = synchronized(out) { out.toString() }
                    text.contains("uid=0")
                }
            } catch (t: Throwable) {
                process?.destroyForcibly()
                false
            }
        }
    }
}
