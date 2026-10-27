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

        // Every retained interval, not just the last [days] days.
        //
        // This used to ask for INTERVAL_DAILY between `cutoff` and `now`, which
        // is the one window that cannot contain the answer: an app unused for
        // 30+ days has no bucket in the last 30 days at all, so it never showed
        // up and the cleaner found nothing to do. Android keeps daily buckets
        // for about a week, weekly for a month, monthly for half a year and
        // yearly for two years, so the longest intervals are the ones that
        // actually know when an app was last opened.
        val lastUsed = mutableMapOf<String, Long>()
        for (interval in INTERVALS) {
            val stats = manager.queryUsageStats(interval, 0L, now) ?: continue
            for (s in stats) {
                val previous = lastUsed[s.packageName] ?: 0L
                if (s.lastTimeUsed > previous) lastUsed[s.packageName] = s.lastTimeUsed
            }
        }

        // No data at all means usage access was just granted (or the stats were
        // wiped), not that every app is unused. Guessing here would clear the
        // caches of apps the user opens daily.
        if (lastUsed.isEmpty()) return emptyList()

        // Only apps the user can actually launch.
        //
        // Taking every installed package would hand the cleaner hundreds of
        // system components that have no usage statistics *because they have
        // no UI* - they would all look "unused for 30+ days" and have their
        // caches cleared, which is not what the cleaner promises.
        val pm = context.packageManager
        return pm.getInstalledPackages(0)
            .map { it.packageName }
            .filter { pkg ->
                if (pkg == context.packageName) return@filter false
                if (pm.getLaunchIntentForPackage(pkg) == null) return@filter false
                // Absent from every bucket means not opened within the
                // retained history, which is the strongest "unused" signal
                // available.
                (lastUsed[pkg] ?: 0L) < cutoff
            }
            .sorted()
    }

    private val INTERVALS = listOf(
        UsageStatsManager.INTERVAL_YEARLY,
        UsageStatsManager.INTERVAL_MONTHLY,
        UsageStatsManager.INTERVAL_WEEKLY,
        UsageStatsManager.INTERVAL_DAILY,
    )
}
