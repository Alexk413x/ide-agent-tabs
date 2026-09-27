# Run by the terminal as `-File`; the agent's command, arguments and prompt arrive in environment variables.
& {
    $c = $env:IDE_AGENT_TABS_COMMAND
    Remove-Item env:IDE_AGENT_TABS_COMMAND -ErrorAction Ignore
    $a = [System.Collections.Generic.List[string]]::new()
    if ($null -ne $env:IDE_AGENT_TABS_ARGS) {
        # pwsh's ConvertFrom-Json turns date-like strings into DateTime, which would reformat an argument.
        if ($PSVersionTable.PSVersion.Major -ge 7) {
            $parsed = [System.Text.Json.JsonSerializer]::Deserialize($env:IDE_AGENT_TABS_ARGS, [string[]])
        } else {
            # Windows PowerShell emits a JSON array as one object, so it must not be wrapped in @().
            $parsed = $env:IDE_AGENT_TABS_ARGS | ConvertFrom-Json
        }
        foreach ($arg in $parsed) { $a.Add([string]$arg) }
        Remove-Item env:IDE_AGENT_TABS_ARGS
    }
    if ($null -ne $env:IDE_AGENT_TABS_PROMPT) {
        $a.Add($env:IDE_AGENT_TABS_PROMPT)
        Remove-Item env:IDE_AGENT_TABS_PROMPT
    }
    $argv = $a.ToArray()

    # Windows PowerShell and pwsh before 7.3 strip embedded quotes and drop empty arguments on the way to a
    # native program, so there the launcher builds the command line itself and passes it through --%.
    $mode = Get-Variable -Name PSNativeCommandArgumentPassing -ValueOnly -ErrorAction Ignore
    $info = Get-Command -Name $c -ErrorAction Ignore | Select-Object -First 1
    if ($argv.Count -gt 0 -and ($null -eq $mode -or "$mode" -eq 'Legacy') -and $info -and $info.CommandType -eq 'Application') {
        $env:IDE_AGENT_TABS_ARGV = ($argv | ForEach-Object {
                if ($_ -ne '' -and $_ -notmatch '[\s"]') { $_ }
                else { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }
            }) -join ' '
        & $c --% %IDE_AGENT_TABS_ARGV%
        Remove-Item env:IDE_AGENT_TABS_ARGV -ErrorAction Ignore
    } else {
        & $c @argv
    }
}
