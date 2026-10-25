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

        // Detect modes first so the UI is usable immediately. The root probe
        // spawns `su`, which blocks on a Magisk prompt until the user answers.
        detectModes()
        refreshPermissions()
        reload()
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

    fun hasAllFilesAccess(): Boolean =
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            android.os.Environment.isExternalStorageManager()
        } else {
            true
        }

    fun onShizukuPermissionResult(granted: Boolean) {
        if (granted) {
            viewModelScope.launch(Dispatchers.IO) {
                PythonBridge.registerAdbRunner(ShizukuRunner())
            }
        }
        refreshPermissions()
        reload()
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
                reload()
            }
            return
        }
        _state.update { it.copy(mode = mode, scan = null, clean = null, selected = emptySet()) }
        reload()
    }

    fun reload() {
        viewModelScope.launch {
            _state.update { it.copy(phase = Phase.LOADING, error = null, message = null) }
            try {
                val mode = _state.value.mode
                val (backend, cleaners) = CleanerRepository.cleaners(context, mode)
                _state.update {
                    it.copy(
                        phase = Phase.IDLE,
                        backend = backend,
                        cleaners = cleaners,
                        selected = cleaners.filter { c -> c.enabled }.map { c -> c.name }.toSet(),
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

    fun scan() {
        viewModelScope.launch {
            val current = _state.value
            _state.update {
                it.copy(phase = Phase.SCANNING, error = null, message = null, clean = null)
            }
            try {
                val unused = unusedPackagesFor(current.selectedCleaners)
                val summary = CleanerRepository.scan(
                    context, current.mode, current.selectedCleaners, unused,
                )
                _state.update { it.copy(phase = Phase.IDLE, scan = summary) }
            } catch (t: Throwable) {
                _state.update { it.copy(phase = Phase.IDLE, error = t.message) }
            }
        }
    }

    fun clean(dryRun: Boolean) {
        viewModelScope.launch {
            val current = _state.value
            _state.update { it.copy(phase = Phase.CLEANING, error = null, message = null) }
            try {
                val unused = unusedPackagesFor(current.selectedCleaners)
                val summary = CleanerRepository.apply(
                    context, current.mode, current.selectedCleaners, dryRun, unused,
                )
                _state.update {
                    it.copy(
                        phase = Phase.IDLE,
                        clean = summary,
                        message = if (dryRun) {
                            "Zkušební režim: uvolnilo by se ${formatBytes(summary.freedBytes)}"
                        } else {
                            "Uvolněno ${formatBytes(summary.freedBytes)} (${summary.deleted} souborů)"
                        },
                    )
                }
            } catch (t: Throwable) {
                _state.update { it.copy(phase = Phase.IDLE, error = t.message) }
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