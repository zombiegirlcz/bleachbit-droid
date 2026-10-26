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
 *
 * Since the shell session rewrite, commands are NOT run by spawning `su` each
 * time: [session] keeps one `su` process alive and feeds commands to its stdin.
 * Spawning a new `su` per command produced a Magisk prompt for every `test -e`
 * and `stat` during a scan and buried the screen in dialogs.
 */
class RootRunner : ShellRunner {

    override fun run(command: String): String = session.exec(command).toJson()

    companion object {
        private const val TAG = "RootRunner"

        /** Short: a pending Magisk prompt must not block startup. */
        private const val PROBE_TIMEOUT_SEC = 5L

        /** The one persistent root shell, shared by every backend call. */
        private val session = ShellSession(TAG) {
            val process = Runtime.getRuntime().exec("su")
            // `su` reads commands from stdin; keep the stream open for the
            // whole session instead of writing `exit` after every command.
            DataOutputStream(process.outputStream).let { stdin ->
                stdin.writeBytes("echo __BB_READY__\n")
                stdin.flush()
            }
            process
        }

        /** Drop the shell (e.g. after the user revoked root). */
        fun reset() = session.close()

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
         * Reuses the persistent session so the probe does not itself trigger a
         * prompt when root was already granted.
         */
        fun isAvailable(): Boolean {
            val r = session.exec("id", PROBE_TIMEOUT_SEC)
            if (r.code != 0) {
                session.close()
                Log.i(TAG, "su probe failed: ${r.stderr.ifEmpty { r.stdout }}")
                return false
            }
            return r.stdout.contains("uid=0")
        }

        /** Kept for callers that need a one-shot root check with `su -c`. */
        internal fun oneShot(command: String): String {
            var process: Process? = null
            val result = JSONObject()
            return try {
                process = Runtime.getRuntime().exec("su")
                DataOutputStream(process.outputStream).use { stdin ->
                    stdin.writeBytes(command + "\n")
                    stdin.writeBytes("exit\n")
                    stdin.flush()
                }
                val out = StringBuilder()
                val t = drain(process.inputStream, out)
                val finished = process.waitFor(PROBE_TIMEOUT_SEC, TimeUnit.SECONDS)
                if (!finished) {
                    process.destroyForcibly()
                    result.put("code", -1)
                    result.put("stdout", "")
                    result.put("stderr", "timeout waiting for su")
                } else {
                    t.join(1000)
                    result.put("code", process.exitValue())
                    result.put("stdout", synchronized(out) { out.toString() })
                    result.put("stderr", "")
                }
                result.toString()
            } catch (t: Throwable) {
                process?.destroyForcibly()
                result.put("code", -1)
                result.put("stdout", "")
                result.put("stderr", t.message ?: t.javaClass.simpleName)
                result.toString()
            }
        }
    }
}