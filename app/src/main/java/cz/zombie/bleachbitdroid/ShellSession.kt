package cz.zombie.bleachbitdroid

import android.util.Log
import org.json.JSONObject
import java.io.OutputStream
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** Result of one command executed inside a [ShellSession]. */
data class ShellResult(val code: Int, val stdout: String, val stderr: String) {
    fun toJson(): String = JSONObject().apply {
        put("code", code)
        put("stdout", stdout)
        put("stderr", stderr)
    }.toString()
}

/**
 * One long-lived shell process, reused for every command.
 *
 * Why: spawning `su` or a Shizuku process per command meant a new Magisk
 * permission prompt (and a new binder round trip) for every single `test -e`,
 * `find` and `stat` during a scan. The screen filled with superuser dialogs and
 * the scan crawled. Here the shell is opened once and kept open; each command is
 * written to its stdin and delimited by a marker line, so we know exactly which
 * output belongs to which command.
 *
 * Protocol:
 * ```
 * { <command>
 * } 2>&1; echo "__BB_END_<id>:$?"
 * ```
 * stderr is folded into stdout so the output keeps its order; every line before
 * the marker is the command's output, the number after the marker is its exit
 * code. A command must not contain a bare `exit`: inside the brace group it
 * would terminate the session's own shell and the marker would never arrive
 * (verified against a real `sh`). Backends only ever send probes and
 * `find`/`stat`/`rm`, so this is a note for whoever adds the next one. The reader thread appends everything to [buffer] and [exec] waits for
 * the marker to appear.
 *
 * Thread-safe: [execLock] serializes commands, so concurrent callers queue up
 * instead of interleaving their output.
 */
class ShellSession(
    private val tag: String,
    private val factory: () -> Process,
) {
    private val execLock = ReentrantLock()
    private val bufLock = Object()

    /**
     * One shell generation's output, and only its own.
     *
     * Why not the single shared StringBuilder this used to be: [close] kills
     * the process but cannot stop the reader thread sitting in `read()` with
     * bytes already in flight. That thread kept appending to the shared
     * builder while [start] had just cleared it and started a second reader on
     * the new process -- two threads, one sink. Output arrives in 8192
     * character chunks, so a path got cut in half and a fragment of the other
     * shell's output landed between the halves. That is where
     * `/davendor.qti.iwlan/cache` came from: `/da` + `vendor.qti.iwlan/cache`,
     * with `ta/data/` lost. Such a path can only ever come back from `rm` as
     * "no such file or directory" -- a clean reporting failures for files it
     * never actually named.
     *
     * Each generation now gets its own [Pipe]; a reader whose pipe is no
     * longer the live one drops its data and stops.
     */
    private class Pipe {
        val text = StringBuilder()
    }

    /** The live generation's pipe; guarded by [bufLock]. */
    private var pipe = Pipe()

    private var process: Process? = null
    private var stdin: OutputStream? = null
    private var seq = 0

    private fun start() {
        val p = factory()
        process = p
        stdin = p.outputStream
        val mine = Pipe()
        synchronized(bufLock) { pipe = mine }
        val t = Thread {
            val buf = CharArray(8192)
            try {
                p.inputStream.bufferedReader().use { reader ->
                    while (true) {
                        val n = reader.read(buf)
                        if (n < 0) break
                        val superseded = synchronized(bufLock) {
                            if (pipe !== mine) {
                                true
                            } else {
                                mine.text.append(buf, 0, n)
                                bufLock.notifyAll()
                                false
                            }
                        }
                        // A dropped shell's leftovers belong to nobody.
                        if (superseded) break
                    }
                }
            } catch (_: Throwable) {
                // shell died or stream closed
            } finally {
                synchronized(bufLock) { bufLock.notifyAll() }
            }
        }
        t.isDaemon = true
        t.start()
        Log.i(tag, "shell session started")
    }

    /** Run one command, returning its output and exit code. */
    fun exec(command: String, timeoutSec: Long = DEFAULT_TIMEOUT_SEC): ShellResult =
        execLock.withLock {
            // No "closed" latch here, deliberately. There used to be one, set
            // by [close] and never cleared, so the first timeout, the first
            // exception, a failed root probe or a dead Shizuku binder made
            // every later command return "session closed" for the rest of the
            // process' life — the whole tier stayed dead until the app was
            // restarted, despite [close]'s promise below. A closed session is
            // simply one without a process, and the next command starts a new
            // one.
            try {
                if (process == null) start()
                val mine = synchronized(bufLock) { pipe }
                val id = ++seq
                val marker = "__BB_END_${id}__"
                stdin!!.write("{ $command\n} 2>&1; echo \"$marker:\$?\"\n".toByteArray())
                stdin!!.flush()

                val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(timeoutSec)
                synchronized(bufLock) {
                    while (true) {
                        val text = mine.text.toString()
                        val idx = text.lastIndexOf("$marker:")
                        if (idx >= 0) {
                            val codeStart = idx + marker.length + 1
                            val nl = text.indexOf('\n', codeStart)
                            if (nl > 0) {
                                val code = text.substring(codeStart, nl).trim().toIntOrNull()
                                if (code != null) {
                                    val out = text.substring(0, idx)
                                    mine.text.delete(0, nl + 1)
                                    return ShellResult(code, out, "")
                                }
                            }
                        }
                        val leftMs = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime())
                        if (leftMs <= 0) {
                            close()
                            return ShellResult(-1, "", "timeout after ${timeoutSec}s")
                        }
                        bufLock.wait(leftMs.coerceAtMost(1000L))
                        if (process == null) return ShellResult(-1, "", "shell exited")
                    }
                }
                @Suppress("UNREACHABLE_CODE")
                ShellResult(-1, "", "unreachable")
            } catch (t: Throwable) {
                Log.w(tag, "exec failed: $command", t)
                close()
                ShellResult(-1, "", t.message ?: t.javaClass.simpleName)
            }
        }

    /** Drop the shell; the next [exec] starts a fresh one. */
    fun close() {
        execLock.withLock {
            val p = process
            process = null
            stdin = null
            try {
                p?.destroyForcibly()
            } catch (_: Throwable) {
            }
            synchronized(bufLock) {
                // Retiring the pipe is what actually silences the old reader.
                pipe = Pipe()
                bufLock.notifyAll()
            }
        }
    }

    companion object {
        /** Bounded so a pending Magisk prompt cannot hang the caller forever. */
        const val DEFAULT_TIMEOUT_SEC = 120L
    }
}
