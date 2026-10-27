package cz.zombie.bleachbitdroid

import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

private fun modeLabel(mode: PrivilegeMode): String = when (mode) {
    PrivilegeMode.NONROOT -> "Bez rootu"
    PrivilegeMode.ADB -> "ADB (Shizuku)"
    PrivilegeMode.ROOT -> "Root"
}

private fun riskLabel(risk: String): String = when (risk) {
    "low" -> "nízké riziko"
    "medium" -> "střední riziko"
    "high" -> "vysoké riziko"
    else -> risk
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CleanerScreen(viewModel: MainViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val context = LocalContext.current

    // Deleting is irreversible and the root tier reaches system directories,
    // so "Vyčistit" asks first. "Prohledat" and "Zkusmo" change nothing and
    // stay one tap away.
    var confirming by remember { mutableStateOf(false) }

    if (confirming) {
        val risky = state.cleaners.filter { it.name in state.selected && it.risk == "high" }
        AlertDialog(
            onDismissRequest = { confirming = false },
            title = { Text("Smazat nalezené soubory?") },
            text = {
                Column {
                    Text(
                        "Spustí se ${state.selectedCleaners.size} čističů na úrovni " +
                            "${modeLabel(state.mode)}. Mazání nelze vzít zpět.",
                    )
                    state.scan?.let {
                        Spacer(Modifier.height(8.dp))
                        Text("Poslední prohledání našlo ${formatBytes(it.totalBytes)}.")
                    }
                    if (risky.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            "Vysoké riziko: ${risky.joinToString { it.name }}",
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    confirming = false
                    viewModel.clean(dryRun = false)
                }) { Text("Vyčistit") }
            },
            dismissButton = {
                TextButton(onClick = { confirming = false }) { Text("Zrušit") }
            },
        )
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("BleachBit Droid") }) },
        bottomBar = {
            // Buttons live in the bottom bar so they are always reachable,
            // regardless of how long the cleaner list is.
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                val ready = state.phase == Phase.IDLE && state.selectedCleaners.isNotEmpty()

                OutlinedButton(
                    onClick = { viewModel.scan() },
                    enabled = ready,
                    modifier = Modifier.weight(1f),
                ) { Text("Prohledat") }

                OutlinedButton(
                    onClick = { viewModel.clean(dryRun = true) },
                    enabled = ready,
                    modifier = Modifier.weight(1f),
                ) { Text("Zkusmo") }

                Button(
                    onClick = { confirming = true },
                    enabled = ready,
                    modifier = Modifier.weight(1f),
                ) {
                    if (state.phase == Phase.CLEANING) {
                        CircularProgressIndicator(modifier = Modifier.height(18.dp))
                    } else {
                        Text("Vyčistit")
                    }
                }
            }
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .padding(horizontal = 16.dp)
                .fillMaxSize(),
        ) {
            Spacer(Modifier.height(8.dp))

            Text("Úroveň oprávnění", style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                state.availableModes.forEach { mode ->
                    FilterChip(
                        selected = state.mode == mode,
                        onClick = { viewModel.selectMode(mode) },
                        label = { Text(modeLabel(mode)) },
                    )
                }
            }

            if (state.availableModes.size == 1) {
                Spacer(Modifier.height(6.dp))
                Text(
                    "Shizuku ani root nejsou dostupné — čistí se jen vlastní data aplikace.",
                    style = MaterialTheme.typography.bodySmall,
                )
            }

            // Extra access that unlocks deeper cleaning even in non-root mode.
            if (!state.allFilesAccess && android.os.Build.VERSION.SDK_INT >= 30) {
                TextButton(onClick = {
                    context.startActivity(
                        Intent(
                            Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                            Uri.parse("package:" + context.packageName),
                        )
                    )
                }) { Text("Povolit přístup ke všem souborům") }
            }
            if (!state.usageAccessGranted) {
                TextButton(onClick = {
                    context.startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
                }) { Text("Povolit přístup k využití aplikací") }
            }

            Spacer(Modifier.height(12.dp))

            if (state.phase == Phase.LOADING) {
                CircularProgressIndicator()
                return@Column
            }

            state.scan?.let { scan ->
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.secondaryContainer,
                    ),
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text(
                            "Nalezeno ${formatBytes(scan.totalBytes)} v ${scan.entries} položkách",
                            fontWeight = FontWeight.Bold,
                        )
                        if (scan.unknownSizeEntries > 0) {
                            Text(
                                "${scan.unknownSizeEntries} položek (cache aplikací) — velikost se ukáže po vyčištění",
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                        scan.perCleaner.forEach { (name, bytes) ->
                            Text(
                                "$name: ${formatBytes(bytes)}",
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
                Spacer(Modifier.height(12.dp))
            }

            // Live progress: without it a scan over Shizuku or su looks frozen.
            if (state.progress.isNotEmpty()) {
                ProgressPanel(
                    title = state.progressTitle,
                    lines = state.progress,
                    running = state.phase == Phase.SCANNING || state.phase == Phase.CLEANING,
                )
                Spacer(Modifier.height(12.dp))
            }

            state.message?.let {
                Text(it, color = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.height(8.dp))
            }
            state.error?.let {
                Text("Chyba: $it", color = MaterialTheme.colorScheme.error)
                Spacer(Modifier.height(8.dp))
            }

            LazyColumn(modifier = Modifier.weight(1f)) {
                items(state.cleaners, key = { it.name }) { cleaner ->
                    CleanerRow(
                        cleaner = cleaner,
                        checked = cleaner.name in state.selected,
                        onToggle = { viewModel.toggleCleaner(cleaner.name) },
                    )
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
private fun ProgressPanel(title: String?, lines: List<String>, running: Boolean) {
    val listState = rememberLazyListState()

    // Follow the tail the way a terminal does, so the newest step is visible.
    LaunchedEffect(lines.size) {
        if (lines.isNotEmpty()) listState.scrollToItem(lines.lastIndex)
    }

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (running) {
                    CircularProgressIndicator(modifier = Modifier.height(16.dp))
                    Spacer(Modifier.width(8.dp))
                }
                Text(
                    title ?: if (running) "Probíhá…" else "Průběh",
                    style = MaterialTheme.typography.titleSmall,
                )
            }
            Spacer(Modifier.height(6.dp))
            Box(Modifier.fillMaxWidth().heightIn(max = 180.dp)) {
                LazyColumn(state = listState) {
                    items(lines) { line ->
                        Text(
                            line,
                            style = MaterialTheme.typography.bodySmall,
                            fontFamily = FontFamily.Monospace,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun CleanerRow(cleaner: CleanerItem, checked: Boolean, onToggle: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(cleaner.description, style = MaterialTheme.typography.bodyLarge)
            Text(
                "${cleaner.name} • ${riskLabel(cleaner.risk)}" +
                    if (!cleaner.enabled) " • chybí: ${cleaner.missingLabels.joinToString()}" else "",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        Switch(
            checked = checked && cleaner.enabled,
            enabled = cleaner.enabled,
            onCheckedChange = { onToggle() },
        )
    }
}