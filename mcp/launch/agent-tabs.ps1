# The PowerShell twin of the sh launcher `agent-tabs`, for Windows without Git Bash: finds Python and runs
# agent_tabs.py with the same arguments, and keeps its exit code. Pipe a request in for `jev choose`:
#   Get-Content -Raw -Encoding UTF8 request.json | & agent-tabs.ps1 jev choose
# Output goes straight to the console handles, so it isn't captured by `$x = & agent-tabs.ps1 ...`.
$main = Join-Path $PSScriptRoot 'agent_tabs.py'
$py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($py) {
  $exe = $py.Source
  $lead = @('-3', '-I', '-S', $main)
} else {
  # The Microsoft Store puts python3 and python stubs on PATH that open the Store instead of running Python.
  $found = foreach ($name in 'python3.exe', 'python.exe') {
    Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*WindowsApps*' } | Select-Object -First 1
  }
  $found = $found | Select-Object -First 1
  if (-not $found) {
    [Console]::Error.WriteLine('{"error":"Agent Tabs needs Python 3.9 or later. Install it from https://www.python.org/downloads/ and run the command again."}')
    exit 127
  }
  $exe = $found.Source
  $lead = @('-I', '-S', $main)
}
# Windows PowerShell 5.1 drops the quotes inside an argument that it passes to a program, so the command line is
# built by hand, with the escaping that CommandLineToArgvW undoes.
function ConvertTo-Argument([string]$Text) {
  if ($Text -ne '' -and $Text -notmatch '[\s"]') { return $Text }
  $quoted = [regex]::Replace($Text, '(\\*)"', '$1$1\"')
  '"' + [regex]::Replace($quoted, '(\\+)$', '$1$1') + '"'
}
$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $exe
$info.Arguments = (@($lead) + @($args) | ForEach-Object { ConvertTo-Argument ([string]$_) }) -join ' '
$info.UseShellExecute = $false
$piped = $MyInvocation.ExpectingInput
if ($piped) {
  $info.RedirectStandardInput = $true
}
# Windows PowerShell 5.1 opens the child's stdin with the console input encoding, and a UTF-8 console (code page
# 65001) makes it send a byte order mark ahead of the input. A UTF-8 encoding without one is set for the start.
$inputEncoding = $null
if ($piped) {
  try {
    $inputEncoding = [Console]::InputEncoding
    [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
  } catch {
    $inputEncoding = $null
  }
}
try {
  $process = [System.Diagnostics.Process]::Start($info)
} finally {
  if ($inputEncoding) {
    try { [Console]::InputEncoding = $inputEncoding } catch {}
  }
}
if ($piped) {
  # Bytes, not StandardInput.Write: Windows PowerShell 5.1 would encode the text in the console code page.
  $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes((@($input) -join "`n"))
  $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
  $process.StandardInput.Close()
}
$process.WaitForExit()
exit $process.ExitCode
