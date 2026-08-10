$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 4173
$localUrl = "http://127.0.0.1:$port/"
$publicUrl = "https://niuzipai-gif.github.io/doubao-watermark-lab/"

function Test-LocalBackend {
  $client = [System.Net.Sockets.TcpClient]::new()
  try {
    $task = $client.ConnectAsync("127.0.0.1", $port)
    $task.Wait(250) | Out-Null
    return $client.Connected
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

try {
  if (-not (Test-LocalBackend)) {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    Start-Process -FilePath $python -ArgumentList @("server.py") -WorkingDirectory $root -WindowStyle Hidden | Out-Null
  }

  for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try {
      $response = Invoke-WebRequest -Uri $localUrl -UseBasicParsing -TimeoutSec 2
      if ($response.StatusCode -eq 200) { break }
    } catch {
      Start-Sleep -Milliseconds 350
    }
  }

  Start-Process $publicUrl
} catch {
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show(
    "Doubao backend failed to start: $($_.Exception.Message)`n`nPlease confirm Python and the project dependencies are installed.",
    "Doubao Watermark Lab",
    "OK",
    "Error"
  ) | Out-Null
}
