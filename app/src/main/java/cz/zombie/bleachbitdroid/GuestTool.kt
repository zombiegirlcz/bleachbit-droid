package cz.zombie.bleachbitdroid

import android.content.Context
import android.util.Log
import java.io.File
import java.io.InputStream
import java.util.concurrent.TimeUnit

/**
 * Runs bundled glibc tools (`shred`, `sqlite3`, `find`) as the app's own uid.
 *
 * There is no `execve` on a glibc binary here: Android cannot start an ELF
 * whose interpreter is `/lib/ld-linux-aarch64.so.1`. Instead the bionic
 * `libelf_loader.so` is started and it own-loads the guest binary plus its
 * glibc dependencies into a private scope, then jumps to its entry point.
 *
 * This means the tools run with exactly the app's privileges and SELinux
 * domain. No root, no Shizuku, no ADB. That is deliberate: it is the non-root
 * tier, and it is the only tier that needs no special access at all.
 *
 * Streams are drained on daemon threads and every wait is bounded, for the
 * same reason as in [RootRunner] and [ShizukuRunner]: a hung child must surface
 * as a failure, never as a frozen UI.
 */
object GuestTool {

    private const val TAG = "GuestTool"
    private const val TIMEOUT_SEC = 120L

    /** Names of tools bundled in `assets/rootfs/usr/bin`. */
    enum class Tool(val fileName: String) {
        SHRED("shred"),
        SQLITE3("sqlite3"),
        FIND("find"),
    }

    /** Result of one guest command. */
    data class Result(
        val exitCode: Int,
        val stdout: String,
        val stderr: String,
        val timedOut: Boolean = false,
    ) {
        val ok: Boolean get() = exitCode == 0 && !timedOut
    }

    /** True when the loader and the rootfs are both present. */
    fun isAvailable(context: Context): Boolean = RootfsInstaller.isReady(context)

    /** Absolute path of a bundled guest binary. */
    fun path(context: Context, tool: Tool): File =
        File(RootfsInstaller.rootfsDir(context), "usr/bin/${tool.fileName}")

    /**
     * Run a guest tool with [args].
     *
     * @param tool which bundled binary to run
     * @param args command line arguments
     * @param stdin optional text written to the child's stdin
     */
    fun run(
        context: Context,
        tool: Tool,
        args: List<String>,
        stdin: String? = null,
    ): Result {
        if (!isAvailable(context)) {
            return Result(-1, "", "guest tools not installed")
        }

        val rootfs = RootfsInstaller.ensure(context)
        val loader = RootfsInstaller.loaderPath(context)
        val binary = path(context, tool)

        val command = buildList {
            add(loader.absolutePath)
            add("--ownall")
            add(binary.absolutePath)
            addAll(args)
        }

        var process: Process? = null
        return try {
            process = ProcessBuilder(command)
                .apply {
                    environment()["ELF_ROOTFS"] = rootfs.absolutePath
                    // Keep the loader quiet unless we are debugging.
                    environment().remove("ELF_DEBUG")
                }
                .start()

            if (stdin != null) {
                process.outputStream.bufferedWriter().use { it.write(stdin) }
            } else {
                process.outputStream.close()
            }

            val out = StringBuilder()
            val err = StringBuilder()
            val outThread = drain(process.inputStream, out)
            val errThread = drain(process.errorStream, err)

            val finished = process.waitFor(TIMEOUT_SEC, TimeUnit.SECONDS)
            if (!finished) {
                process.destroyForcibly()
                outThread.join(1000)
                errThread.join(1000)
                Log.w(TAG, "${tool.fileName} timed out: $args")
                return Result(-1, out.toString(), err.toString(), timedOut = true)
            }

            outThread.join(2000)
            errThread.join(2000)
            Result(process.exitValue(), out.toString(), err.toString())
        } catch (t: Throwable) {
            process?.destroyForcibly()
            Log.w(TAG, "${tool.fileName} failed", t)
            Result(-1, "", t.message ?: t.javaClass.simpleName)
        }
    }

    // ---- convenience wrappers -------------------------------------------------

    /** Report the size in bytes of [path] using GNU `du -sb`. */
    fun measureBytes(context: Context, path: String): Long? {
        val r = run(context, Tool.FIND, listOf(path, "-maxdepth", "0", "-printf", "%s"))
        if (!r.ok) return null
        return r.stdout.trim().toLongOrNull()
    }

    /** Securely overwrite and remove one file. */
    fun shred(context: Context, path: String): Result =
        run(context, Tool.SHRED, listOf("-vzu", path))

    /** Run a SQL statement against [db]. */
    fun sqlite(context: Context, db: String, sql: String): Result =
        run(context, Tool.SQLITE3, listOf(db, sql))

    /** `VACUUM` a database and report how much it shrank. */
    fun vacuum(context: Context, db: String): Result {
        val file = File(db)
        val before = if (file.isFile) file.length() else 0L
        val r = sqlite(context, db, "VACUUM;")
        if (!r.ok) return r
        val after = if (file.isFile) file.length() else 0L
        val saved = (before - after).coerceAtLeast(0L)
        return r.copy(stdout = "vacuum: $before -> $after (-$saved bytes)")
    }

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
                // child died or stream closed
            }
        }
        t.isDaemon = true
        t.start()
        return t
    }
}
