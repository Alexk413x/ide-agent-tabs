& {
    $c = $env:IDE_AGENT_TABS_COMMAND
    Remove-Item env:IDE_AGENT_TABS_COMMAND -ErrorAction Ignore
    $a = [System.Collections.Generic.List[string]]::new()
    # A variable that is set but empty counts as unset, so a launcher that cannot remove one can empty it.
    if ($env:IDE_AGENT_TABS_ARGS) {
        # pwsh's ConvertFrom-Json turns date-like strings into DateTime, which would reformat an argument.
        if ($PSVersionTable.PSVersion.Major -ge 7) {
            $parsed = [System.Text.Json.JsonSerializer]::Deserialize($env:IDE_AGENT_TABS_ARGS, [string[]])
        } else {
            # Windows PowerShell emits a JSON array as one object, so it must not be wrapped in @().
            $parsed = $env:IDE_AGENT_TABS_ARGS | ConvertFrom-Json
        }
        foreach ($arg in $parsed) { $a.Add([string]$arg) }
    }
    Remove-Item env:IDE_AGENT_TABS_ARGS -ErrorAction Ignore
    if ($env:IDE_AGENT_TABS_PROMPT) { $a.Add($env:IDE_AGENT_TABS_PROMPT) }
    Remove-Item env:IDE_AGENT_TABS_PROMPT -ErrorAction Ignore
    $argv = $a.ToArray()

    # cmd.exe parses a .cmd or .bat file's command line, so "a&b" in an argument would run b. For those files the
    # launcher quotes every argument itself and starts the file through "cmd.exe /d /v:off /s /c", where cmd.exe
    # sees only quoted text: a quote is doubled (with the backslashes before it doubled too, for the program the
    # batch file starts), and a percent sign is written as %%cd:~,% so cmd.exe cannot expand %NAME%. A line break
    # cannot cross cmd.exe at all and becomes a space.
    function ConvertTo-BatchArg([string]$Value) {
        $sb = [System.Text.StringBuilder]::new('"')
        $backslashes = 0
        foreach ($ch in ($Value -replace "`r`n|`r|`n", ' ').ToCharArray()) {
            if ($ch -eq '\') {
                $backslashes++
            } else {
                if ($ch -eq '"') { [void]$sb.Append('\' * $backslashes).Append('"') }
                elseif ($ch -eq '%') { [void]$sb.Append('%%cd:~,') }
                $backslashes = 0
            }
            [void]$sb.Append($ch)
        }
        [void]$sb.Append('\' * $backslashes).Append('"')
        $sb.ToString()
    }

    # Windows PowerShell and pwsh before 7.3 strip embedded quotes and drop empty arguments on the way to a
    # native program, so there the launcher builds the command line itself and passes it through --%.
    $mode = Get-Variable -Name PSNativeCommandArgumentPassing -ValueOnly -ErrorAction Ignore
    $info = Get-Command -Name $c -ErrorAction Ignore | Select-Object -First 1
    if ($argv.Count -gt 0 -and $info -and $info.CommandType -eq 'Application' -and [System.IO.Path]::GetExtension($info.Source) -in '.cmd', '.bat') {
        $env:IDE_AGENT_TABS_ARGV = '/d /v:off /s /c "' + ((@($info.Source) + $argv | ForEach-Object { ConvertTo-BatchArg $_ }) -join ' ') + '"'
        & ([System.Environment]::SystemDirectory + '\cmd.exe') --% %IDE_AGENT_TABS_ARGV%
        Remove-Item env:IDE_AGENT_TABS_ARGV -ErrorAction Ignore
    } elseif ($argv.Count -gt 0 -and ($null -eq $mode -or "$mode" -eq 'Legacy') -and $info -and $info.CommandType -eq 'Application') {
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
