package cz.zombie.bleachbitdroid

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
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

@Composable
fun CleanerScreen(viewModel: MainViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()

    Scaffold(
        topBar = { TopAppBar(title = { Text("BleachBit Droid") }) },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .padding(horizontal = 16.dp)
                .fillMaxWidth(),
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

            Spacer(Modifier.height(12.dp))

            when (state.phase) {
                Phase.LOADING -> CircularProgressIndicator()
                else -> {
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

                    Row(
                        modifier = Modifier.fillMaxWidth().padding(vertical = 12.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        OutlinedButton(
                            onClick = { viewModel.scan() },
                            enabled = state.phase == Phase.IDLE && state.selectedCleaners.isNotEmpty(),
                        ) { Text("Prohledat") }

                        OutlinedButton(
                            onClick = { viewModel.clean(dryRun = true) },
                            enabled = state.phase == Phase.IDLE && state.selectedCleaners.isNotEmpty(),
                        ) { Text("Zkusmo") }

                        Button(
                            onClick = { viewModel.clean(dryRun = false) },
                            enabled = state.phase == Phase.IDLE && state.selectedCleaners.isNotEmpty(),
                        ) {
                            if (state.phase == Phase.CLEANING) {
                                CircularProgressIndicator(modifier = Modifier.height(18.dp))
                            } else {
                                Text("Vyčistit")
                            }
                        }
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
                    if (!cleaner.enabled) " • chybí: ${cleaner.missing.joinToString()}" else "",
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
