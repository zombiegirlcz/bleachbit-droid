package cz.zombie.bleachbitdroid

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class Phase { IDLE, LOADING, SCANNING, CLEANING }

data class UiState(
    val phase: Phase = Phase.IDLE,
    val mode: PrivilegeMode = PrivilegeMode.NONROOT,
    val availableModes: List<PrivilegeMode> = listOf(PrivilegeMode.NONROOT),
    val backend: BackendInfo? = null,
    val cleaners: List<CleanerItem> = emptyList(),
    val selected: Set<String> = emptySet(),
    val scan: ScanSummary? = null,
    val clean: CleanSummary? = null,
    val message: String? = null,
    val error: String? = null,
    val usageAccessGranted: Boolean = false,
    val allFilesAccess: Boolean = false,
    /** Live progress lines, newest last; capped so the UI never grows forever. */
    val progress: List<String> = emptyList(),
    /** Current cleaner being scanned/cleaned, for the header. */
    val progressTitle: String? = null,
) {
    val selectedCleaners: List<String>
        get() = cleaners.filter { it.name in selected && it.enabled }.map { it.name }
}

private data class Access(
    val shizuku: Boolean,
    val root: Boolean,
    val usageStats: Boolean,
    val allFiles: Boolean,
)

/**
 * All Shizuku work happens off the main thread.
 *
 * This is not a nicety: a binder call from the main thread propagates the
 * caller's StrictMode thread policy to the callee. `shizuku_server` then runs
 * its permission check (an HTTP request) on that same thread and dies with
 * `NetworkOnMainThreadException`, which it reports back as "app has no
 * permission" even when the grant exists.
 */
class MainViewModel(app: Application) : AndroidViewModel(app) {

    private companion object {
        const val TAG = "MainViewModel"

        /** Enough history to see what happened, small enough to not choke UI. */
        const val MAX_PROGRESS_LINES = 200
    }

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private val context get() = getApplication<Application>()

    init {
        PythonBridge.init(context)

        // The Shizuku binder is delivered *after* the activity starts, so a
        // one-shot check at startup would never see it. React to binder and
        // permission events instead of polling.
        ShizukuRunner.onBinderChanged = { refreshPermissions() }
        ShizukuRunner.startListeners()

        // Register the VACUUM bridge *before* any reload: `supports_sqlite` is
        // derived from whether a runner was injected, and the reload() below
        // asks the registry straight away. Without this
        // the UI listed own_databases as "missing sqlite3" even though the
        // bundled sqlite3 works (GuestSelfTest proves it a moment later).
        // Registration itself touches no disk; VacuumRunner re-checks
        // GuestTool.isAvailable() at call time, so it is safe to do here.
        PythonBridge.registerVacuumRunner(VacuumRunner(context))

        // Install the bundled glibc toolchain and prove it runs, off the main
        // thread: extraction touches disk and the smoke test spawns processes.
        viewModelScope.launch(Dispatchers.IO) {
            RootfsInstaller.ensure(context)
            GuestSelfTest.run(context)
        }

        // Exactly once each. This used to call detectModes() and reload()
        // twice - here and again at the end of the IO block above - which did
        // all the Python work twice at startup and, worse, the late reload()
        // reset `selected`, so a cleaner the user had just unticked came back
        // checked a second later.
        reload(resetSelection = true)
        // Ends with detectModes(); the root probe spawns `su`, which blocks on
        // a Magisk prompt until the user answers, so it stays off the main
        // thread.
        refreshPermissions()
    }

    /**
     * Re-read runtime permissions, (re-)register shell runners and re-detect
     * available privilege levels. Runs entirely on IO: `hasPermission()` is a
     * binder call and `RootRunner.isAvailable()` spawns a process.
     */
    fun refreshPermissions() {
        viewModelScope.launch {
            val access = withContext(Dispatchers.IO) {
                Log.i(TAG, "refresh: checking shizuku")
                val shizuku = ShizukuRunner.hasPermission()
                Log.i(TAG, "refresh: shizuku=$shizuku")
                if (shizuku) {
                    PythonBridge.registerAdbRunner(ShizukuRunner())
                    Log.i(TAG, "refresh: adb runner registered")
                }
                // Bounded probe: a pending Magisk prompt must not stall startup.
                Log.i(TAG, "refresh: probing root")
                val root = RootRunner.isAvailable()
                Log.i(TAG, "refresh: root=$root")
                if (root) {
                    PythonBridge.registerRootRunner(RootRunner())
                }
                Access(
                    shizuku = shizuku,
                    root = root,
                    usageStats = UsageStatsProvider.hasPermission(context),
                    allFiles = hasAllFilesAccess(),
                )
            }

            Log.i(TAG, "refresh: done $access")
            _state.update {
                it.copy(
                    usageAccessGranted = access.usageStats,
                    allFilesAccess = access.allFiles,
                )
            }
            detectModes()
        }
    }


    fun onShizukuPermissionResult(granted: Boolean) {
        if (granted) {
            viewModelScope.launch(Dispatchers.IO) {
                PythonBridge.registerAdbRunner(ShizukuRunner())
            }
        }
        refreshPermissions()
        reload(resetSelection = true)
    }

    private fun detectModes() {
        viewModelScope.launch {
            val modes = try {
                val json = withContext(Dispatchers.IO) {
                    PythonBridge.detect(context, _state.value.mode.id)
                }
                val arr = json.optJSONArray("available")
                buildList {
                    add(PrivilegeMode.NONROOT)
                    if (arr != null) {
                        for (i in 0 until arr.length()) {
                            when (arr.getString(i)) {
                                "adb" -> add(PrivilegeMode.ADB)
                                "root" -> add(PrivilegeMode.ROOT)
                            }
                        }
                    }
                }
            } catch (t: Throwable) {
                listOf(PrivilegeMode.NONROOT)
            }
            _state.update { it.copy(availableModes = modes.distinct()) }
        }
    }

    fun selectMode(mode: PrivilegeMode) {
        if (_state.value.mode == mode) return
        if (mode == PrivilegeMode.ADB) {
            viewModelScope.launch {
                val granted = withContext(Dispatchers.IO) { ShizukuRunner.hasPermission() }
                if (!granted) {
                    ShizukuRunner.requestPermission()
                    return@launch
                }
                _state.update {
                    it.copy(mode = mode, scan = null, clean = null, selected = emptySet())
                }
                reload(resetSelection = true)
            }
            return
        }
        _state.update { it.copy(mode = mode, scan = null, clean = null, selected = emptySet()) }
        reload(resetSelection = true)
    }

    /**
     * Re-read the cleaner registry for the current tier.
     *
     * [resetSelection] is for the cases where the old selection has no
     * meaning any more — app start and a tier switch. Otherwise the selection
     * survives: a reload triggered by `onResume` used to silently re-check
     * every default, so a cleaner the user had just unticked came back.
     */
    fun reload(resetSelection: Boolean = false) {
        viewModelScope.launch {
            _state.update { it.copy(phase = Phase.LOADING, error = null, message = null) }
            try {
                val mode = _state.value.mode
                val (backend, cleaners) = CleanerRepository.cleaners(context, mode)
                _state.update { current ->
                    // HIGH-risk cleaners start unchecked. They are shared
                    // scratch space someone put there on purpose (or a
                    // one-off dataset), so the user must opt in explicitly
                    // instead of losing it to a stray tap on "Vyčistit".
                    val default = cleaners
                        .filter { c -> c.enabled && c.risk != "high" }
                        .map { c -> c.name }
                        .toSet()
                    // A reload must not undo the user's ticks; it only drops
                    // names that this tier cannot run any more.
                    val enabled = cleaners.filter { it.enabled }.map { it.name }.toSet()
                    val keep = current.selected intersect enabled
                    current.copy(
                        phase = Phase.IDLE,
                        backend = backend,
                        cleaners = cleaners,
                        selected = if (resetSelection) default else keep,
                    )
                }
            } catch (t: Throwable) {
                _state.update { it.copy(phase = Phase.IDLE, error = t.message) }
            }
        }
    }

    fun toggleCleaner(name: String) {
        _state.update { current ->
            val sel = current.selected.toMutableSet()
            if (!sel.add(name)) sel.remove(name)
            current.copy(selected = sel, scan = null, clean = null)
        }
    }

    /** Package list for cleaners that only touch apps unused for N days. */
    private suspend fun unusedPackagesFor(names: List<String>): List<String>? {
        val days = _state.value.cleaners
            .filter { it.name in names && it.unusedDays > 0 }
            .minOfOrNull { it.unusedDays }
            ?: return null

        return withContext(Dispatchers.IO) { UsageStatsProvider.unusedPackages(context, days) }
    }

    private fun pushProgress(event: ProgressEvent) {
        val line = event.render()
        val title = when (event.type) {
            "cleaner_start" -> event.description.ifEmpty { event.cleaner }
            "cleaner_done" -> null
            else -> null
        }
        _state.update { current ->
            val lines = (current.progress + line).takeLast(MAX_PROGRESS_LINES)
            current.copy(
                progress = lines,
                progressTitle = title ?: current.progressTitle,
            )
        }
    }

    fun scan() {
        viewModelScope.launch {
            val current = _state.value
            _state.update {
                it.copy(
                    phase = Phase.SCANNING,
                    error = null,
                    message = null,
                    clean = null,
                    progress = emptyList(),
                    progressTitle = null,
                )
            }
            try {
                val unused = unusedPackagesFor(current.selectedCleaners)
                val summary = CleanerRepository.scan(
                    context, current.mode, current.selectedCleaners, unused,
                    onProgress = { pushProgress(it) },
                )
                _state.update {
                    it.copy(phase = Phase.IDLE, scan = summary, progressTitle = null)
                }
            } catch (t: Throwable) {
                _state.update {
                    it.copy(phase = Phase.IDLE, error = t.message, progressTitle = null)
                }
            }
        }
    }

    fun clean(dryRun: Boolean) {
        viewModelScope.launch {
            val current = _state.value
            _state.update {
                it.copy(
                    phase = Phase.CLEANING,
                    error = null,
                    message = null,
                    progress = emptyList(),
                    progressTitle = null,
                )
            }
            try {
                val unused = unusedPackagesFor(current.selectedCleaners)
                val summary = CleanerRepository.apply(
                    context, current.mode, current.selectedCleaners, dryRun, unused,
                    onProgress = { pushProgress(it) },
                )
                _state.update {
                    it.copy(
                        phase = Phase.IDLE,
                        clean = summary,
                        progressTitle = null,
                        message = buildString {
                            if (dryRun) {
                                append("Zkušební režim: uvolnilo by se ")
                                append(formatBytes(summary.freedBytes))
                            } else {
                                append("Uvolněno ${formatBytes(summary.freedBytes)}")
                                append(" (${summary.deleted} souborů)")
                            }
                            // The ADB tier clears app caches without being able
                            // to measure them, so saying just "0 B" would be a
                            // lie; say what we know and what we do not.
                            if (summary.unknown > 0) {
                                append(" + ${summary.unknown} položek neznámé velikosti")
                            }
                            if (summary.failed > 0) {
                                append(", ${summary.failed} selhalo")
                                // Point at the record instead of making the
                                // user retype a reason the panel has scrolled
                                // past. Only when something actually failed.
                                summary.auditPath?.let { append("\nZáznam: $it") }
                            }
                        },
                    )
                }
            } catch (t: Throwable) {
                _state.update {
                    it.copy(phase = Phase.IDLE, error = t.message, progressTitle = null)
                }
            }
        }
    }
}

fun formatBytes(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    val units = listOf("KB", "MB", "GB")
    var value = bytes.toDouble() / 1024
    var idx = 0
    while (value >= 1024 && idx < units.lastIndex) {
        value /= 1024
        idx++
    }
    return String.format("%.1f %s", value, units[idx])
}