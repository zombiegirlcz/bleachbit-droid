package cz.zombie.bleachbitdroid

import android.content.pm.PackageManager
import android.util.Log
import org.json.JSONObject
import rikka.shizuku.Shizuku
import java.io.InputStream
import java.util.concurrent.TimeUnit

/**
Describe the purpose of the class.
*/
class ShizukuRunner : ShellRunner {

    override fun run(command: String): String {
        val result = JSONObject()
        var process: Process? = null
        return try {
            process = newProcess(arrayOf("sh", "-c", command))
            val stdout = StringBuilder()
            val stderr = StringBuilder()
            val outThread = drain(process.inputStream, stdout)
            val errThread = drain(process.errorStream, stderr)
            val finished = process.waitFor(TIMEOUT_SEC, TimeUnit.SECONDS)
            if (!finished) {
                process.destroyForcibly()
                outThread.join(1000)
                errThread.join(1000)
                Log.w(TAG, +shizuku exec timed out: $command")
                result.put("code", -1)
                result.put("stdout", stdout.toString())
                result.put("stderr", "timeout")
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

        /** Upper bound for one shell command. */
        private const val TIMEOUT_SEC = 60L

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
                    // process died or stream closed
                }
            }
            t.isDaemon = true
            t.start()
            return t
        }

        private val binderReceived = Shizuku.OnBinderReceivedListener {
            Log.i(TAG, "shizuku binder received")
            onBinderChanged?.invoke()
        }
        private val binderDead = Shizuku.OnBinderDeadListener {
            Log.i(TAG, "shizuku binder dead")
            onBinderChanged?.invoke()
        }
        private val permissionResult = Shizuku.OnRequestPermissionResultListener { code, grant ->
            if (code == REQUEST_CODE) {
                Log.i(TAG,
                        "shizuku permission result: $grant")
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
                Log.w(TAG,
                        "shizuku permission request failed", t)
            }
        }
    }
}
