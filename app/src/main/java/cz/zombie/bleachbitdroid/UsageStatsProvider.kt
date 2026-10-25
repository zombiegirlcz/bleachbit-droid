package cz.zombie.bleachbitdroid

import android.app.AppOpsManager
import android.app.usage.UsageStatsManager
import android.content.Context
import android.os.Process
import java.util.concurrent.TimeUnit

/**
 * Answers "which apps have not been used for N days?".
 *
 * Needs `PACKAGE_USAGE_STATS` granted through Settings > Special access >
 * Usage access. Without it the provider returns `null` rather than guessing —
 * the Python side then skips the cleaner instead of clearing the wrong apps.
 */
object UsageStatsProvider {

    fun hasPermission(context: Context): Boolean {
        val appOps = context.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = appOps.unsafeCheckOpNoThrow(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            Process.myUid(),
            context.packageName,
        )
        return mode == AppOpsManager.MODE_ALLOWED
    }

    /**
     * Package names that were not in the foreground for at least [days] days.
     * Returns `null` when usage access is not granted.
     */
    fun unusedPackages(context: Context, days: Int): List<String>? {
        if (!hasPermission(context)) return null

        val manager = context.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val now = System.currentTimeMillis()
        val cutoff = now - TimeUnit.DAYS.toMillis(days.toLong())

        // Everything the device saw in the window; last time used is per package.
        val stats = manager.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, cutoff, now)
            ?: return emptyList()

        val lastUsed = mutableMapOf<String, Long>()
        for (s in stats) {
            val prev = lastUsed[s.packageName]
            if (prev == null || s.lastTimeUsed > prev) {
                lastUsed[s.packageName] = s.lastTimeUsed
            }
        }

        val installed = context.packageManager.getInstalledPackages(0)
            .map { it.packageName }
            .toSet()

        return lastUsed
            .filter { (pkg, whenUsed) ->
                pkg in installed && whenUsed in 1 until cutoff && pkg != context.packageName
            }
            .keys
            .sorted()
    }
}
