package cz.zombie.bleachbitdroid

import android.app.Application
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

class MainViewModel(app: Application) : AndroidViewModel(app) {

    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private val context get() = getApplication<Application>()

    init {
        PythonBridge.init(context)
        if (ShizukuRunner.hasPermission()) {
            PythonBridge.registerAdbRunner(ShizukuRunner())
        }
        registerRootIfPossible()
        refreshPermissions()
        detectModes()
        reload()
    }

    /** `su` probe is a blocking process spawn — keep it off the UI thread. */
    private fun registerRootIfPossible() {
        viewModelScope.launch(Dispatchers.IO) {
            if (RootRunner.isAvailable()) {
                PythonBridge.registerRootRunner(RootRunner())
                detectModes()
            }
        }
    }

    fun refreshPermissions() {
        _state.update {
            it.copy(
                usageAccessGranted = UsageStatsProvider.hasPermission(context),
                allFilesAccess = hasAllFilesAccess(),
            )
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
            PythonBridge.registerAdbRunner(ShizukuRunner())
        }
        detectModes()
        reload()
    }

    private fun detectModes() {
        viewModelScope.launch {
            val modes = try {
                val json = PythonBridge.detect(context, _state.value.mode.id)
                val arr = json.optJSONArray("available")
                buildList {
                    add(PrivilegeMode.NONROOT)
                    if (arr != null) {
                        for (i in 0 until arr.length()) {
                            when (arr.getString(i)) {
                                "adb" -> if (ShizukuRunner.hasPermission()) add(PrivilegeMode.ADB)
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
        if (mode == PrivilegeMode.ADB && !ShizukuRunner.hasPermission()) {
            ShizukuRunner.requestPermission()
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

    /** Package lists computed on the Kotlin side, keyed by cleaner name. */
    private suspend fun unusedPackagesFor(names: List<String>): List<String>? {
        val current = _state.value
        val needsUsage = current.cleaners.any {
            it.name in names && it.kind == "package" && it.unusedDays > 0
        }
        if (!needsUsage) return null

        val days = current.cleaners
            .filter { it.name in names && it.unusedDays > 0 }
            .minOfOrNull { it.unusedDays }
            ?: return null

        return withContext(Dispatchers.IO) { UsageStatsProvider.unusedPackages(context, days) }
    }

    fun scan() {
        viewModelScope.launch {
            val current = _state.value
            _state.update { it.copy(phase = Phase.SCANNING, error = null, message = null, clean = null) }
            try {
                val unused = unusedPackagesFor(current.selectedCleaners)
                val summary = CleanerRepository.scan(
                    context, current.mode, current.selectedCleaners, unused
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
