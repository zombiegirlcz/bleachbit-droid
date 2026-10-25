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
)

data class BackendInfo(
    val name: String,
    val capabilities: List<String>,
)

data class ScanSummary(
    val totalBytes: Long,
    val entries: Int,
    val perCleaner: Map<String, Long>,
    val raw: JSONObject,
)

data class CleanSummary(
    val dryRun: Boolean,
    val freedBytes: Long,
    val deleted: Int,
    val failed: Int,
    val logLines: List<String>,
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
            )
        }
        info to cleaners
    }

    suspend fun scan(
        context: Context,
        mode: PrivilegeMode,
        cleaners: List<String>,
    ): ScanSummary = withContext(Dispatchers.Default) {
        val json = PythonBridge.scan(context, mode.id, cleaners)
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
            raw = json,
        )
    }

    suspend fun apply(
        context: Context,
        mode: PrivilegeMode,
        cleaners: List<String>,
        dryRun: Boolean,
    ): CleanSummary = withContext(Dispatchers.Default) {
        val json = PythonBridge.applyClean(context, mode.id, cleaners, dryRun)
        json.optString("error").takeIf { it.isNotEmpty() }?.let { error(it) }

        val lines = mutableListOf<String>()
        val log = json.getJSONArray("log")
        for (i in 0 until log.length()) {
            lines += log.getJSONObject(i).toString()
        }

        CleanSummary(
            dryRun = json.getBoolean("dry_run"),
            freedBytes = json.getLong("freed_bytes"),
            deleted = json.getInt("deleted"),
            failed = json.getInt("failed"),
            logLines = lines,
        )
    }

    private fun JSONArray.toStringList(): List<String> =
        (0 until length()).map { getString(it) }
}
