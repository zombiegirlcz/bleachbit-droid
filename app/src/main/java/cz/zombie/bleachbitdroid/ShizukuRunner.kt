package cz.zombie.bleachbitdroid

import android.content.pm.PackageManager
import android.util.Log
import rikka.shizuku.Shizuku
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Executes shell commands as the `shell` user (uid 2000) through Shizuku.
 *
 * The process is created once and kept alive by [ShellSession]: creating a
 * Shizuku process per command cost a binder round trip each time and made a
 * scan crawl. One remote `sh` now serves the whole session.
 *
 * [waitForExit] exists because `Process.waitFor(timeout, unit)` polls
 * `exitValue()`, and `ShizukuRemoteProcess.exitValue()` throws
 * `IllegalArgumentException: process hasn't exited` while the process is still
 * running. That made every command fail with exit code -1.
 */
class ShizukuRunner : ShellRunner {

    override fun run(command: String): String = session.exec(command).toJson()

    companion object {
        private const val TAG = "ShizukuRunner"
        const val REQUEST_CODE = 421

        /** Upper bound for one shell command. */
        private const val TIMEOUT_SEC = 120L

        private val session = ShellSession(TAG) {
            // Plain `sh` reading from stdin: it executes commands as they arrive
            // and, unlike a `while read` loop, understands multi-line constructs
            // (`{ cmd\n} 2>&1; echo marker:$?`), which the session protocol uses.
            newProcess(arrayOf("sh"))
        }

        fun reset() = session.close()

        /**
         * Wait for [process] to exit, bounded by [seconds].
         *
         * See the class comment: a timeout-based `waitFor` cannot be used with
         * a Shizuku remote process.
         */
        private fun waitForExit(process: Process, seconds: Long): Boolean {
            val done = CountDownLatch(1)
            val waiter = Thread {
                try {
                    process.waitFor()
                } catch (_: Throwable) {
                    // process died or the binder went away
                } finally {
                    done.countDown()
                }
            }
            waiter.isDaemon = true
            waiter.start()
            return done.await(seconds, TimeUnit.SECONDS)
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

        private val binderReceived = Shizuku.OnBinderReceivedListener {
            Log.i(TAG, "shizuku binder received")
            onBinderChanged?.invoke()
        }
        private val binderDead = Shizuku.OnBinderDeadListener {
            Log.i(TAG, "shizuku binder dead")
            reset()
            onBinderChanged?.invoke()
        }
        private val permissionResult = Shizuku.OnRequestPermissionResultListener { code, grant ->
            if (code == REQUEST_CODE) {
                Log.i(TAG, "shizuku permission result: $grant")
                onBinderChanged?.invoke()
            }
        }

        @Volatile
        var onBinderChanged: (() -> Unit)? = null

        fun startListeners() {
            try {
                Shizuku.addBinderReceivedListenerSticky(binderReceived)
                Shizuku.addBinderDeadListener(binderDead)
                Shizuku.addRequestPermissionResultListener(permissionResult)
            } catch (t: Throwable) {
                Log.w(TAG, "cannot register shizuku listeners", t)
            }
        }

        fun isAvailable(): Boolean = try {
            Shizuku.pingBinder()
        } catch (t: Throwable) {
            false
        }

        fun hasPermission(): Boolean = try {
            isAvailable() &&
                !Shizuku.isPreV11() &&
                Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
        } catch (t: Throwable) {
            false
        }

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