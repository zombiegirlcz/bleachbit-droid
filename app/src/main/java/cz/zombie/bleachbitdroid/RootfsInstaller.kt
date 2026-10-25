package cz.zombie.bleachbitdroid

import android.content.Context
import android.util.Log
import java.io.File

/**
 * Extracts the bundled glibc rootfs from assets into the app's files dir.
 *
 * Why not keep it in `nativeLibraryDir`? The libraries are named `libc.so.6`,
 * `ld-linux-aarch64.so.1` and so on. Android only extracts files under
 * `lib/<abi>/` whose name ends in `.so`, so these would be dropped, and
 * renaming them would break the loader's `DT_NEEDED` / soname resolution.
 *
 * Keeping the original names inside `filesDir` works because the loader never
 * `execve`s them: it maps them itself with `mmap(PROT_EXEC)`. The Android 10+
 * ban on executing files from the app data dir therefore does not apply.
 *
 * Only `libelf_loader.so` (a bionic binary) goes to `jniLibs`, so Android
 * extracts it and the app is allowed to `execve` it.
 */
object RootfsInstaller {

    private const val TAG = "RootfsInstaller"
    private const val ASSET_ROOT = "rootfs"
    private const val VERSION_MARKER = ".rootfs-version"

    /** Bump when the bundled rootfs changes so installs re-extract. */
    private const val VERSION = 1

    fun rootfsDir(context: Context): File = File(context.filesDir, ASSET_ROOT)

    fun loaderPath(context: Context): File =
        File(context.applicationInfo.nativeLibraryDir, "libelf_loader.so")

    /** Extract the rootfs if needed. Safe to call often. */
    fun ensure(context: Context): File {
        val root = rootfsDir(context)
        val marker = File(root, VERSION_MARKER)

        if (marker.isFile && marker.readText().trim() == VERSION.toString()) {
            return root
        }

        Log.i(TAG, "extracting rootfs v$VERSION -> ${root.absolutePath}")
        if (root.exists()) root.deleteRecursively()
        if (!root.mkdirs()) error("cannot create ${root.absolutePath}")

        val assets = context.assets
        val pending = ArrayDeque(listOf(ASSET_ROOT))
        var files = 0
        while (pending.isNotEmpty()) {
            val dir = pending.removeFirst()
            val entries = assets.list(dir) ?: continue
            for (entry in entries) {
                val assetPath = "$dir/$entry"
                val children = assets.list(assetPath)
                if (children != null && children.isNotEmpty()) {
                    pending.addLast(assetPath)
                    continue
                }
                val relative = assetPath.removePrefix("$ASSET_ROOT/")
                val target = File(root, relative)
                target.parentFile?.mkdirs()
                assets.open(assetPath).use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }
                if (relative.startsWith("usr/bin/")) {
                    target.setExecutable(true, false)
                }
                target.setReadable(true, false)
                files++
            }
        }

        marker.writeText(VERSION.toString())
        Log.i(TAG, "rootfs ready: $files files")
        return root
    }

    fun isReady(context: Context): Boolean {
        val libc = File(rootfsDir(context), "usr/lib/aarch64-linux-gnu/libc.so.6")
        return loaderPath(context).isFile && libc.isFile
    }
}
