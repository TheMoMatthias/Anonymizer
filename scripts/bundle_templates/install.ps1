$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Unblocking files (clears the Windows security warning)..."
Get-ChildItem -Path $here -Recurse | Unblock-File -ErrorAction SilentlyContinue

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$desktop\Document Anonymizer.lnk")
$shortcut.TargetPath = Join-Path $here "launch.bat"
$shortcut.WorkingDirectory = $here
$shortcut.IconLocation = Join-Path $here "python\runtime\python.exe"
$shortcut.Save()

Write-Host "Setup complete. A 'Document Anonymizer' shortcut was added to your Desktop."
Write-Host "Double-click it (or launch.bat in this folder) to start the app."
