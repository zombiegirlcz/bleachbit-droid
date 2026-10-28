package cz.zombie.bleachbitdroid

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

enum class PrivilegeMode(val id: String) {
    NONROOT("nonroot"),
    ADB("adb"),
    ROOT("root"),
}

data class CleanerItem(
    val name: String,
    val description: String,
    val risk: String,
    val requires: List<String>,
    val enabled: Boolean,
    val missing: List<String>,
    /** Why it cannot run, in Czech; the raw [missing] tokens are internal. */
    val missingLabels: List<String>,
    val kind: String,
    val unusedDays: Int,
)

data class BackendInfo(
    val name: String,
    val capabilities: List<String>,
    val supportsPackages: Boolean,
)

data class ScanSummary(
    val totalBytes: Long,
    val entries: Int,
    val perCleaner: Map<String, Long>,
    val unknownSizeEntries: Int,
    val raw: JSONObject,
)

data class CleanSummary(
    val dryRun: Boolean,
    val freedBytes: Long,
    val deleted: Int,
    val failed: Int,
    /** Cleared successfully, but the tier could not measure how much. */
    val unknown: Int,
    val logLines: List<String>,
    /** Where the run was written down, so a failure can be quoted later. */
    val auditPath: String? = null,
)

/** Parses the JSON documents coming out of the Python core. */
object CleanerRepository {

    suspend fun cleaners(
        context: Context,
        mode: PrivilegeMode,
    ): Pair<BackendInfo, List<CleanerItem>> = withContext(Dispatchers.Default) {
        val json = PythonBridge.listCleaners(context, mode.id)
        json.optString("error").takeIf { it.isNotEmpty() }?.let { error(it) }

        val backend = json.getJSONObject("backend")
        val info = BackendInfo(
            name = backend.getString("name"),
            capabilities = backend.getJSONArray("capabilities").toStringList(),
            supportsPackages = backend.optBoolean("supports_packages", false),
        )

        val cleaners = mutableListOf<CleanerItem>()
        val arr = json.getJSONArray("cleaners")
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            cleaners += CleanerItem(
                name = o.getString("name"),
                description = o.getString("description"),
                risk = o.getString("risk"),
                requires = o.getJSONArray("requires").toStringList(),
                enabled = o.getBoolean("enabled"),
                missing = o.getJSONArray("missing").toStringList(),
                missingLabels = o.optJSONArray("missing_labels")?.toStringList()
                    ?: o.getJSONArray("missing").toStringList(),
                kind = o.optString("kind", "path"),
                unusedDays = o.optInt("unused_days", 0),
            )
        }
        info to cleaners
    }

    suspend fun scan(
        context: Context,
        mode: PrivilegeMode,
        cleaners: List<String>,
        unusedPackages: List<String>? = null,
        onProgress: ((ProgressEvent) -> Unit)? = null,
    ): ScanSummary = withContext(Dispatchers.Default) {
        val sink = ProgressSink(
            { onProgress?.invoke(it) },
            CleanAudit(context.filesDir, "scan-${mode.id}"),
        )
        val json = PythonBridge.scanStreaming(
            context, mode.id, cleaners, unusedPackages, sink,
        )
        json.optString("error").takeIf { it.isNotEmpty() }?.let { error(it) }

        val perCleaner = mutableMapOf<String, Long>()
        val entries = json.getJSONArray("entries")
        for (i in 0 until entries.length()) {
            val e = entries.getJSONObject(i)
            perCleaner.merge(e.getString("cleaner"), e.getLong("size"), Long::plus)
        }

        ScanSummary(
            totalBytes = json.getLong("total_bytes"),
            entries = entries.length(),
            perCleaner = perCleaner,
            unknownSizeEntries = json.optInt("unknown_size_entries", 0),
            raw = json,
        )
    }

    suspend fun apply(
        context: Context,
        mode: PrivilegeMode,
        cleaners: List<String>,
        dryRun: Boolean,
        unusedPackages: List<String>? = null,
        onProgress: ((ProgressEvent) -> Unit)? = null,
    ): CleanSummary = withContext(Dispatchers.Default) {
        val audit = CleanAudit(
            context.filesDir,
            if (dryRun) "dryrun-${mode.id}" else "clean-${mode.id}",
        )
        val sink = ProgressSink({ onProgress?.invoke(it) }, audit)
        val json = PythonBridge.applyCleanStreaming(
            context, mode.id, cleaners, dryRun, unusedPackages, sink,
        )
        json.optString("error").takeIf { it.isNotEmpty() }?.let { error(it) }

        val lines = mutableListOf<String>()
        val log = json.getJSONArray("log")
        for (i in 0 until log.length()) {
            val record = log.getJSONObject(i).toString()
            lines += record
            // The structured log carries the per-entry `reason`, which is the
            // part that actually explains a failed run.
            audit.write(record)
        }

        CleanSummary(
            dryRun = json.getBoolean("dry_run"),
            freedBytes = json.getLong("freed_bytes"),
            deleted = json.getInt("deleted"),
            failed = json.getInt("failed"),
            unknown = json.optInt("unknown", 0),
            logLines = lines,
            auditPath = audit.path,
        )
    }

    private fun JSONArray.toStringList(): List<String> =
        (0 until length()).map { getString(it) }
}
