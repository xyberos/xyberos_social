<#
.SYNOPSIS
    Rehearses a backup and restore against the Compose PostgreSQL instance.

.DESCRIPTION
    Dumps a source database, restores the dump into a scratch database, and compares the
    row count of every table plus the applied migration revision between the two. The
    restore target is refused unless its name looks like a scratch database, so a live
    database cannot be overwritten by a mistyped parameter.

    The dump and restore run inside the container with its own pg_dump and pg_restore, so no
    PostgreSQL client tools are required on the host. The source database is only read.

    Exits 0 on a clean restore, 1 when any table or the migration revision differs.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File ops/rehearse-restore.ps1

.EXAMPLE
    # Rehearse against a database captured by a different container or role.
    powershell -ExecutionPolicy Bypass -File ops/rehearse-restore.ps1 `
        -Container soccom-postgres-1 -User soccom -SourceDatabase soccom -TargetDatabase soccom_restore_rehearsal
#>
[CmdletBinding()]
param(
    [string]$Container = "soccom-postgres-1",
    [string]$User = "soccom",
    [string]$SourceDatabase = "soccom",
    [string]$TargetDatabase = "soccom_test",
    [string]$DumpPath = "/tmp/soccom-restore-rehearsal.dump"
)

$ErrorActionPreference = "Stop"

if ($TargetDatabase -eq $SourceDatabase) {
    throw "Refusing to rehearse: the restore target and the source are both '$SourceDatabase'."
}
if ($TargetDatabase -notmatch "test|scratch|rehearsal") {
    throw "Refusing to restore into '$TargetDatabase': its name must contain test, scratch, or rehearsal."
}

function Invoke-Psql {
    param([string]$Database, [string]$Sql)
    $output = docker exec $Container psql -U $User -d $Database -At -v ON_ERROR_STOP=1 -c $Sql
    if ($LASTEXITCODE -ne 0) {
        throw "psql failed against database '$Database'."
    }
    return $output
}

function Get-TableCounts {
    param([string]$Database)
    $counts = @{}
    $tables = Invoke-Psql -Database $Database -Sql "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    foreach ($table in $tables) {
        if (-not $table) { continue }
        $counts[$table] = [int](Invoke-Psql -Database $Database -Sql "SELECT count(*) FROM $table")
    }
    return $counts
}

$running = docker inspect -f "{{.State.Running}}" $Container 2>$null
if ($running -ne "true") {
    throw "Container '$Container' is not running. Start it with: docker compose up -d postgres"
}

Write-Host "==> Dumping '$SourceDatabase' from '$Container'"
docker exec $Container rm -f $DumpPath | Out-Null
docker exec $Container pg_dump -U $User -d $SourceDatabase --format=custom --file=$DumpPath
if ($LASTEXITCODE -ne 0) {
    throw "pg_dump failed."
}
$dumpBytes = [int](docker exec $Container sh -c "wc -c < $DumpPath")
Write-Host ("    dump size: {0:N0} bytes" -f $dumpBytes)

Write-Host "==> Recreating '$TargetDatabase'"
Invoke-Psql -Database "postgres" -Sql "DROP DATABASE IF EXISTS $TargetDatabase WITH (FORCE)" | Out-Null
Invoke-Psql -Database "postgres" -Sql "CREATE DATABASE $TargetDatabase" | Out-Null

Write-Host "==> Restoring into '$TargetDatabase'"
docker exec $Container pg_restore -U $User -d $TargetDatabase --exit-on-error $DumpPath
if ($LASTEXITCODE -ne 0) {
    throw "pg_restore failed."
}

Write-Host "==> Comparing '$SourceDatabase' with '$TargetDatabase'"
$sourceCounts = Get-TableCounts -Database $SourceDatabase
$restoredCounts = Get-TableCounts -Database $TargetDatabase
$sourceRevision = Invoke-Psql -Database $SourceDatabase -Sql "SELECT version_num FROM alembic_version"
$restoredRevision = Invoke-Psql -Database $TargetDatabase -Sql "SELECT version_num FROM alembic_version"

$failures = @()
if (-not $sourceCounts.Count) {
    $failures += "the source database has no tables to compare"
}
foreach ($table in $sourceCounts.Keys) {
    if (-not $restoredCounts.ContainsKey($table)) {
        $failures += "$table is missing from the restored database"
    }
    elseif ($sourceCounts[$table] -ne $restoredCounts[$table]) {
        $failures += "$table has $($sourceCounts[$table]) rows in the source and $($restoredCounts[$table]) restored"
    }
}
if ($sourceRevision -ne $restoredRevision) {
    $failures += "migration revision is '$sourceRevision' in the source and '$restoredRevision' restored"
}

if ($failures.Count) {
    Write-Host ""
    Write-Host "FAIL: the restored database does not match the source" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  - $_" }
    exit 1
}

$tablesCompared = $sourceCounts.Count
$epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$dropHint = 'docker exec {0} psql -U {1} -d postgres -c "DROP DATABASE {2} WITH (FORCE)"' -f `
    $Container, $User, $TargetDatabase
Write-Host ""
Write-Host "PASS: $tablesCompared tables restored with identical row counts, revision '$restoredRevision'." -ForegroundColor Green
Write-Host "      Restored database '$TargetDatabase' was left in place for inspection."
Write-Host "      Drop it with: $dropHint"
Write-Host "      Record the rehearsal so the staleness alert can see it:"
Write-Host "        soccom_restore_rehearsal_timestamp_seconds $epoch"
exit 0
