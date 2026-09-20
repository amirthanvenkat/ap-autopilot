<#
.SYNOPSIS
  Start, stop or inspect the local PostgreSQL used by the integration tests.

.DESCRIPTION
  The integration suite needs a real PostgreSQL: the design depends on data
  modifying CTEs, FOR UPDATE SKIP LOCKED, advisory locks and jsonb, none of
  which have a useful substitute.

  This drives a portable PostgreSQL installed under the user's profile. It
  needs no administrator rights, registers no service, and listens on
  loopback only at a non-default port so it cannot collide with a system
  PostgreSQL installed later. Trust authentication is fine here because the
  server is unreachable from outside the machine; do not copy this
  configuration anywhere that is.

.EXAMPLE
  ./scripts/local-postgres.ps1 start
  ./scripts/local-postgres.ps1 status
  ./scripts/local-postgres.ps1 stop
#>
param(
  [ValidateSet('start', 'stop', 'status', 'url')]
  [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'

$Bin  = Join-Path $env:USERPROFILE '.local\pgsql\bin'
$Data = Join-Path $env:USERPROFILE '.local\pgdata'
$Log  = Join-Path $env:USERPROFILE '.local\pgdata.log'
$Port = 5433
$Db   = 'ap_autopilot_test'
$Url  = "postgresql://postgres@127.0.0.1:$Port/$Db"

if (-not (Test-Path $Bin)) {
  Write-Error "PostgreSQL binaries not found at $Bin. Download the Windows binaries zip from get.enterprisedb.com and extract it to $env:USERPROFILE\.local."
}

switch ($Action) {
  'start' {
    if (-not (Test-Path $Data)) {
      & "$Bin\initdb.exe" -D $Data -U postgres --auth-host=trust --auth-local=trust -E UTF8 --locale=C | Out-Null
      Add-Content -Path "$Data\postgresql.conf" -Encoding utf8 -Value @"

port = $Port
listen_addresses = '127.0.0.1'
max_connections = 50
fsync = off
synchronous_commit = off
full_page_writes = off
"@
    }
    # Detached, so stopping whatever launched this does not take the server
    # down with it.
    Start-Process -FilePath "$Bin\pg_ctl.exe" `
      -ArgumentList @('-D', "`"$Data`"", '-l', "`"$Log`"", 'start') -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
      Start-Sleep -Milliseconds 500
      & "$Bin\pg_isready.exe" -h 127.0.0.1 -p $Port 2>&1 | Out-Null
      if ($LASTEXITCODE -eq 0) { break }
    }
    & "$Bin\pg_isready.exe" -h 127.0.0.1 -p $Port
    & "$Bin\psql.exe" -h 127.0.0.1 -p $Port -U postgres -tAc `
      "select 1 from pg_database where datname = '$Db'" | Out-Null
    if (-not $?) { }
    $exists = & "$Bin\psql.exe" -h 127.0.0.1 -p $Port -U postgres -tAc `
      "select count(*) from pg_database where datname = '$Db'"
    if ($exists.Trim() -eq '0') {
      & "$Bin\createdb.exe" -h 127.0.0.1 -p $Port -U postgres $Db
      Write-Output "created database $Db"
    }
    Write-Output "TEST_DATABASE_URL=$Url"
  }
  'stop' {
    & "$Bin\pg_ctl.exe" -D $Data -m fast stop
  }
  'status' {
    & "$Bin\pg_isready.exe" -h 127.0.0.1 -p $Port
  }
  'url' {
    Write-Output $Url
  }
}
