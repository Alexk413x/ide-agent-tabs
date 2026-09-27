param([Parameter(Mandatory = $true)][string]$SpecPath)

& {
    param([string]$SpecPath)

    # pwsh's ConvertFrom-Json turns date-like strings into DateTime, which would change a prompt or an
    # argument, so pwsh parses with System.Text.Json. Windows PowerShell has no such conversion.
    function ConvertFrom-JsonElement($e) {
        switch ($e.ValueKind.ToString()) {
            'Object' {
                $o = [ordered]@{}
                foreach ($p in $e.EnumerateObject()) { $o[$p.Name] = ConvertFrom-JsonElement $p.Value }
                return [pscustomobject]$o
            }
            'Array' { return , @(foreach ($i in $e.EnumerateArray()) { ConvertFrom-JsonElement $i }) }
            'String' { return $e.GetString() }
            'Null' { return $null }
            default { return $e.GetRawText() }
        }
    }

    try {
        $text = [System.IO.File]::ReadAllText($SpecPath, [System.Text.Encoding]::UTF8)
        [System.IO.File]::Delete($SpecPath)
        if ('System.Text.Json.JsonDocument' -as [type]) {
            $doc = [System.Text.Json.JsonDocument]::Parse($text)
            $spec = ConvertFrom-JsonElement $doc.RootElement
            $doc.Dispose()
        } else {
            $spec = $text | ConvertFrom-Json
        }
        if ($spec.version -ne 1) { throw "unsupported launch spec version $($spec.version)" }
        if ($spec.pidFile) { [System.IO.File]::WriteAllText($spec.pidFile, [string]$PID) }
        Set-Location -LiteralPath $spec.cwd -ErrorAction Stop
    } catch {
        Write-Error "ide-agent-tabs: could not start the agent: $_"
        return
    }

    foreach ($pair in @($spec.env)) {
        if ($null -ne $pair) { [System.Environment]::SetEnvironmentVariable($pair.name, $pair.value) }
    }
    $env:IDE_AGENT_TABS_ID = $spec.id
    $env:IDE_AGENT_TABS_AGENT = $spec.agent

    $a = [System.Collections.Generic.List[string]]::new()
    foreach ($arg in @($spec.args)) { if ($null -ne $arg) { $a.Add([string]$arg) } }
    if ($null -ne $spec.prompt) { $a.Add([string]$spec.prompt) }
    $c = [string]$spec.command
    $argv = $a.ToArray()

    # Windows PowerShell and pwsh before 7.3 mangle embedded quotes and drop empty arguments on the way to a
    # native program. There the launcher builds the program's command line itself and hands it over through
    # --% and an environment variable, which PowerShell expands once and never parses.
    $mode = Get-Variable -Name PSNativeCommandArgumentPassing -ValueOnly -ErrorAction Ignore
    $info = Get-Command -Name $c -ErrorAction Ignore | Select-Object -First 1
    if ($argv.Count -gt 0 -and ($null -eq $mode -or "$mode" -eq 'Legacy') -and $info -and $info.CommandType -eq 'Application') {
        $env:IDE_AGENT_TABS_ARGV = ($argv | ForEach-Object {
                if ($_ -ne '' -and $_ -notmatch '[\s"]') { $_ }
                else { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }
            }) -join ' '
        & $c --% %IDE_AGENT_TABS_ARGV%
        Remove-Item -Path env:IDE_AGENT_TABS_ARGV -ErrorAction Ignore
    } else {
        & $c @argv
    }
} $SpecPath
