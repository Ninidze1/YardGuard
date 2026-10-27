<#
.SYNOPSIS
    YardGuard installer: sets up everything YardGuard needs inside its own folder.

.DESCRIPTION
    Started by YardGuard.cmd the first time (and by Update.cmd after an update). Installs, all
    inside the YardGuard folder and without admin rights:
        runtime\python   the official python.org Python 3.12 (NuGet package, signed by the PSF)
        .venv            YardGuard's libraries (exact versions and SHA-256 hashes: requirements.txt)
        tools\ffmpeg     ffmpeg + ffprobe for the camera video (pinned build, SHA-256 checked)
        .cache           downloads, pip cache, temp files and the log (.cache\setup.log)
    Every step is skipped when it is already done, so running it again continues where it stopped.

.PARAMETER Unattended
    No window and no questions: progress goes to the console, problems end the script (exit 10).
.PARAMETER Update
    Reinstall the libraries after a "git pull" or a new download (keeps settings and data).
.PARAMETER NoLaunch
    Do not start YardGuard at the end.
.PARAMETER Torch
    auto (default: CUDA build when an NVIDIA GPU is found), cuda, or cpu.
.PARAMETER PipCache
    Folder for the pip download cache (default .cache\pip).
.PARAMETER AssumeNoGpu
    Testing aid: choose PyTorch as on a PC without an NVIDIA graphics card.

Exit codes: 0 done, 10 failed (the user was told why), 11 cancelled.
#>
[CmdletBinding()]
param(
    [switch]$Unattended,
    [switch]$Update,
    [switch]$NoLaunch,
    [ValidateSet('auto', 'cuda', 'cpu')][string]$Torch = 'auto',
    [string]$PipCache = '',
    [switch]$AssumeNoGpu    # testing aid: behave as on a PC without an NVIDIA GPU
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# --- Paths ------------------------------------------------------------------------------------

$Root       = Split-Path -Parent $PSScriptRoot
$Cache      = Join-Path $Root '.cache'
$Downloads  = Join-Path $Cache 'downloads'
$Tmp        = Join-Path $Cache 'tmp'
$LogFile    = Join-Path $Cache 'setup.log'
$RuntimeDir = Join-Path $Root 'runtime\python'
$BasePy     = Join-Path $RuntimeDir 'python.exe'
$Venv       = Join-Path $Root '.venv'
$VenvPy     = Join-Path $Venv 'Scripts\python.exe'
$VenvPyw    = Join-Path $Venv 'Scripts\pythonw.exe'
$Pending    = Join-Path $Venv 'yardguard-setup.pending'
$StateFile  = Join-Path $Venv 'yardguard-setup.json'
$FfmpegDir  = Join-Path $Root 'tools\ffmpeg'
$Reqs       = Join-Path $Root 'requirements.txt'
$Versions   = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'versions.json') -Raw | ConvertFrom-Json
if (-not $PipCache) { $PipCache = Join-Path $Cache 'pip' }

$Steps = @(
    @{ Name = 'Checking this PC';               Weight = 2  },
    @{ Name = "Downloading Python $($Versions.python.version)"; Weight = 6 },
    @{ Name = 'Creating the Python environment'; Weight = 4 },
    @{ Name = 'Installing the AI libraries';     Weight = 65 },
    @{ Name = 'Downloading the ffmpeg video tools'; Weight = 18 },
    @{ Name = 'Checking the installation';       Weight = 5 }
)
$script:StepIndex = 0
$script:Cancelled = $false
$script:Ui = $null
$script:Warnings = New-Object System.Collections.Generic.List[string]

# --- Environment: everything stays inside the YardGuard folder ---------------------------------

foreach ($d in @($Cache, $Downloads, $Tmp, $PipCache)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
$env:TEMP = $Tmp
$env:TMP = $Tmp
$env:PIP_CACHE_DIR = $PipCache
$env:YOLO_CONFIG_DIR = Join-Path $Cache 'ultralytics'
$env:TORCH_HOME = Join-Path $Cache 'torch'
$env:CUDA_CACHE_PATH = Join-Path $Cache 'cuda'
$env:MPLCONFIGDIR = Join-Path $Cache 'matplotlib'
$env:YOLO_AUTOINSTALL = 'false'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
foreach ($name in @('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'PYTHONSTARTUP')) {
    Remove-Item -LiteralPath "env:$name" -ErrorAction SilentlyContinue
}
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# --- Logging and errors -----------------------------------------------------------------------

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $Message
    try { [IO.File]::AppendAllText($LogFile, $line + "`r`n", [Text.Encoding]::UTF8) } catch { }
    if (-not $script:Ui) { Write-Host $line }
}

function Fail([string]$Message, [string]$Advice = '') {
    $e = New-Object System.Exception $Message
    $e.Data['advice'] = $Advice
    throw $e
}

function Add-Warning([string]$Message) {
    $script:Warnings.Add($Message)
    Write-Log "WARNING: $Message"
}

# --- Progress window (WinForms; console only with -Unattended or if it cannot open) ------------

function Start-Ui {
    if ($Unattended) { return }
    try {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        [System.Windows.Forms.Application]::EnableVisualStyles()
        $font = New-Object System.Drawing.Font('Segoe UI', 10)
        $form = New-Object System.Windows.Forms.Form
        $form.Text = 'YardGuard setup'
        $form.ClientSize = New-Object System.Drawing.Size(560, 236)
        $form.FormBorderStyle = 'FixedDialog'
        $form.MaximizeBox = $false
        $form.StartPosition = 'CenterScreen'
        $form.Font = $font
        $form.BackColor = [System.Drawing.Color]::White

        $title = New-Object System.Windows.Forms.Label
        $title.Text = 'Setting up YardGuard'
        $title.Font = New-Object System.Drawing.Font('Segoe UI Semibold', 14)
        $title.SetBounds(20, 16, 520, 32)
        $step = New-Object System.Windows.Forms.Label
        $step.SetBounds(20, 56, 520, 24)
        $bar = New-Object System.Windows.Forms.ProgressBar
        $bar.SetBounds(20, 84, 520, 20)
        $bar.Maximum = 1000
        $detail = New-Object System.Windows.Forms.Label
        $detail.SetBounds(20, 110, 520, 76)
        $detail.ForeColor = [System.Drawing.Color]::FromArgb(90, 98, 112)
        $detail.Text = 'This happens only once and takes about 5 to 20 minutes, depending on your internet ' +
                       'speed. Everything is stored inside the YardGuard folder.'

        $log = New-Object System.Windows.Forms.Button
        $log.Text = 'Open log'
        $log.SetBounds(20, 192, 110, 32)
        $retry = New-Object System.Windows.Forms.Button
        $retry.Text = 'Try again'
        $retry.SetBounds(312, 192, 110, 32)
        $retry.Visible = $false
        $close = New-Object System.Windows.Forms.Button
        $close.Text = 'Cancel'
        $close.SetBounds(430, 192, 110, 32)

        $log.Add_Click({ Start-Process notepad.exe -ArgumentList ('"{0}"' -f $LogFile) })
        $retry.Add_Click({ $script:Choice = 'retry' })
        $close.Add_Click({ $script:Choice = 'close'; $script:Cancelled = $true; $script:Ui.Form.Close() })
        $form.Add_FormClosing({ $script:Cancelled = $true; if (-not $script:Choice) { $script:Choice = 'close' } })
        $form.Controls.AddRange(@($title, $step, $bar, $detail, $log, $retry, $close))
        $form.Show()
        $form.Activate()
        $script:Ui = @{ Form = $form; Title = $title; Step = $step; Bar = $bar; Detail = $detail;
                        Retry = $retry; Close = $close }
        [System.Windows.Forms.Application]::DoEvents()
    } catch {
        $script:Ui = $null
        Write-Log "No setup window ($($_.Exception.Message)): showing progress in the console"
    }
}

function Pump {
    if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
    if ($script:Cancelled) { Fail 'Setup was cancelled.' 'Double-click YardGuard.cmd to continue. Finished steps are kept.' }
}

function Set-Progress([double]$Fraction, [string]$Detail) {
    $done = 0
    for ($i = 0; $i -lt $script:StepIndex - 1; $i++) { $done += $Steps[$i].Weight }
    $total = ($Steps | ForEach-Object { $_.Weight } | Measure-Object -Sum).Sum
    $w = $Steps[[Math]::Max($script:StepIndex - 1, 0)].Weight
    $value = [int](1000 * ($done + $w * [Math]::Min([Math]::Max($Fraction, 0), 1)) / $total)
    if ($script:Ui) {
        $script:Ui.Bar.Value = [Math]::Min($value, 1000)
        if ($Detail) { $script:Ui.Detail.Text = $Detail }
    } elseif ($Detail -and $Detail -ne $script:LastDetail) {
        Write-Host ("    {0,3}%  {1}" -f [int]($value / 10), $Detail)
    }
    $script:LastDetail = $Detail
    if ($env:YARDGUARD_SETUP_SHOTS -and $Fraction -ge 0.4 -and $script:ShotStep -ne $script:StepIndex) {
        $script:ShotStep = $script:StepIndex
        Save-UiShot "step$($script:StepIndex)"
    }
    Pump
}

function Start-Step([int]$Number) {
    $script:StepIndex = $Number
    $text = '{0} ({1}/{2})...' -f $Steps[$Number - 1].Name, $Number, $Steps.Count
    Write-Log "== $text"
    if ($script:Ui) { $script:Ui.Step.Text = $text }
    Set-Progress 0 ''
}

function Save-UiShot([string]$Name) {
    <# Testing aid: with YARDGUARD_SETUP_SHOTS set to a folder, save a picture of the setup window. #>
    $dir = $env:YARDGUARD_SETUP_SHOTS
    if (-not $dir -or -not $script:Ui) { return }
    try {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $f = $script:Ui.Form
        $bmp = New-Object System.Drawing.Bitmap($f.Width, $f.Height)
        $f.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle(0, 0, $f.Width, $f.Height)))
        $bmp.Save((Join-Path $dir "$Name.png"), [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
    } catch { Write-Log "  (could not save a picture of the window: $($_.Exception.Message))" }
}

function Show-Failure([string]$Message, [string]$Advice) {
    if (-not $script:Ui) { return 'close' }
    $ui = $script:Ui
    $ui.Title.Text = 'Setup stopped'
    $ui.Title.ForeColor = [System.Drawing.Color]::FromArgb(185, 28, 28)
    $ui.Step.Text = $Message
    $ui.Detail.Text = $Advice
    $ui.Retry.Visible = $true
    $ui.Close.Text = 'Close'
    $script:Choice = $null
    $script:Cancelled = $false
    [System.Windows.Forms.Application]::DoEvents()
    Save-UiShot 'failed'
    while (-not $script:Choice -and $ui.Form.Visible) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 50
    }
    $choice = $script:Choice
    if ($choice -eq 'retry') {
        $script:Cancelled = $false
        $ui.Title.Text = 'Setting up YardGuard'
        $ui.Title.ForeColor = [System.Drawing.Color]::Black
        $ui.Retry.Visible = $false
        $ui.Close.Text = 'Cancel'
        $script:Choice = $null
    }
    return $choice
}

function Ask-YesNo([string]$Question) {
    if ($Unattended -or -not $script:Ui) { return $true }
    $answer = [System.Windows.Forms.MessageBox]::Show($Question, 'YardGuard setup', 'YesNo', 'Warning')
    return $answer -eq 'Yes'
}

# --- Helpers ----------------------------------------------------------------------------------

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function ConvertTo-Arg([string]$Value) {
    if ($Value -notmatch '[\s"]' -and $Value) { return $Value }
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Read-Shared([string]$Path) {
    # The child process still writes the file: open it with FileShare.ReadWrite
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $fs = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try { return (New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)).ReadToEnd() } finally { $fs.Dispose() }
}

function Invoke-Tool([string]$Exe, [string[]]$Arguments, [scriptblock]$OnLine = $null, [scriptblock]$OnTick = $null) {
    <# Run a console program without a window; feed new output lines to $OnLine; keep the UI alive. #>
    $id = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $out = Join-Path $Tmp "run-$id.out"
    $err = Join-Path $Tmp "run-$id.err"
    $argLine = ($Arguments | ForEach-Object { ConvertTo-Arg $_ }) -join ' '
    Write-Log "> $(ConvertTo-Arg $Exe) $argLine"
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $argLine -WorkingDirectory $Root -NoNewWindow -PassThru `
                           -RedirectStandardOutput $out -RedirectStandardError $err
    } catch {
        $msg = $_.Exception.Message
        if ($msg -match 'Application Control|blocked') {
            Fail "Windows blocked $(Split-Path -Leaf $Exe)." ("Smart App Control or another security policy stopped it. " +
                 "See 'Smart App Control' in README.md. Details: $msg")
        }
        Fail "Could not start $(Split-Path -Leaf $Exe)." $msg
    }
    $null = $p.Handle   # keeps the exit code readable after the process ends
    $seen = 0
    $text = ''
    while ($true) {
        $exited = $p.WaitForExit(200)
        $text = Read-Shared $out
        if ($OnLine) {
            $lines = $text -split "`r?`n"
            $complete = if ($exited) { $lines.Count } else { $lines.Count - 1 }
            for ($i = $seen; $i -lt $complete; $i++) { if ($lines[$i].Trim()) { & $OnLine $lines[$i].Trim() } }
            if ($complete -gt $seen) { $seen = $complete }
        }
        if ($exited) { break }
        if ($OnTick) { & $OnTick }
        try { Pump } catch {
            & taskkill.exe /T /F /PID $p.Id 2>$null | Out-Null
            throw
        }
    }
    $p.WaitForExit()
    $errText = Read-Shared $err
    $all = ($text + "`n" + $errText).Trim()
    if ($all) { try { [IO.File]::AppendAllText($LogFile, $all + "`r`n", [Text.Encoding]::UTF8) } catch { } }
    Remove-Item -LiteralPath $out, $err -ErrorAction SilentlyContinue
    Write-Log "  exit code $($p.ExitCode)"
    return @{ Code = $p.ExitCode; Text = $all }
}

function Get-Download([string]$Url, [string]$Dest, [string]$Sha256, [long]$Size, [string]$Label) {
    <# Download with resume (HTTP Range) and up to 4 tries, then check the SHA-256. #>
    if ((Test-Path -LiteralPath $Dest) -and (Get-Sha256 $Dest) -eq $Sha256) {
        Write-Log "  $Label already downloaded"
        return
    }
    $part = "$Dest.part"
    $mb = [Math]::Round($Size / 1MB)
    for ($try = 1; $try -le 4; $try++) {
        $resp = $null; $in = $null; $outStream = $null
        try {
            $start = 0
            if (Test-Path -LiteralPath $part) { $start = (Get-Item -LiteralPath $part).Length }
            if ($start -ge $Size) { $start = 0; Remove-Item -LiteralPath $part -Force }
            $req = [System.Net.HttpWebRequest]::Create($Url)
            $req.UserAgent = 'YardGuard-setup'
            $req.Timeout = 30000
            $req.ReadWriteTimeout = 60000
            if ($start -gt 0) { $req.AddRange([long]$start) }
            $resp = $req.GetResponse()
            if ($start -gt 0 -and [int]$resp.StatusCode -ne 206) { $start = 0 }
            $mode = if ($start -gt 0) { [IO.FileMode]::Append } else { [IO.FileMode]::Create }
            Write-Log "  downloading $Url (try $try, from byte $start)"
            $in = $resp.GetResponseStream()
            $outStream = [IO.File]::Open($part, $mode, [IO.FileAccess]::Write)
            $buf = New-Object byte[] 262144
            $done = $start
            $clock = [Diagnostics.Stopwatch]::StartNew()
            while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                $outStream.Write($buf, 0, $n)
                $done += $n
                if ($clock.ElapsedMilliseconds -ge 150) {
                    Set-Progress ($done / $Size) ('{0}: {1:N0} of {2:N0} MB' -f $Label, ($done / 1MB), $mb)
                    $clock.Restart()
                }
            }
            $outStream.Dispose(); $outStream = $null
            break
        } catch {
            if ($script:Cancelled) { throw }
            Write-Log "  download problem: $($_.Exception.Message)"
            if ($try -eq 4) {
                Fail "Could not download $Label." ("Check that this PC is online, then click Try again (the download " +
                     "continues where it stopped). Details: $($_.Exception.Message)")
            }
            Start-Sleep -Seconds (3 * $try)
        } finally {
            if ($outStream) { $outStream.Dispose() }
            if ($in) { $in.Dispose() }
            if ($resp) { $resp.Close() }
        }
    }
    Set-Progress 1 "Checking $Label..."
    $hash = Get-Sha256 $part
    if ($hash -ne $Sha256) {
        Remove-Item -LiteralPath $part -Force
        Fail "The downloaded $Label is not the expected file (SHA-256 check failed)." ("Click Try again. If it keeps " +
             "happening, a proxy or antivirus may be changing downloads. Expected $Sha256, got $hash.")
    }
    Move-Item -LiteralPath $part -Destination $Dest -Force
    Write-Log "  $Label OK, SHA-256 $hash"
}

function Expand-Entries([string]$Zip, [string]$Prefix, [string]$Dest, [string[]]$Only = @()) {
    <# Extract the zip entries under $Prefix (optionally only the listed relative paths) into $Dest. #>
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $entries = @($archive.Entries | Where-Object { $_.FullName.StartsWith($Prefix) -and $_.Name })
        $count = 0
        foreach ($entry in $entries) {
            $rel = [Uri]::UnescapeDataString($entry.FullName.Substring($Prefix.Length))
            if ($Only.Count -and $Only -notcontains $rel) { continue }
            $target = Join-Path $Dest ($rel -replace '/', '\')
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
            $count++
            if ($count % 50 -eq 0) { Set-Progress 0.95 "Unpacking... $count files"; }
        }
        return $count
    } finally { $archive.Dispose() }
}

function Test-Signed([string]$Path, [string]$Signer) {
    $sig = Get-AuthenticodeSignature -LiteralPath $Path
    return ($sig.Status -eq 'Valid' -and $sig.SignerCertificate.Subject -like "*$Signer*")
}

function Find-NvidiaSmi {
    $cmd = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $sys = Join-Path $env:WINDIR 'System32\nvidia-smi.exe'
    if (Test-Path -LiteralPath $sys) { return $sys }
    return $null
}

function Read-State {
    if (Test-Path -LiteralPath $StateFile) {
        try { return Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json } catch { }
    }
    return $null
}

function Get-RunningApp {
    <# YardGuard processes started from this folder (their files cannot be replaced while running). #>
    $app = (Join-Path $Root 'app.py').ToLowerInvariant()
    try {
        return @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
                 Where-Object { $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($app) })
    } catch { return @() }
}

# --- Steps ------------------------------------------------------------------------------------

function Step-Check {
    Start-Step 1
    $os = [Environment]::OSVersion.Version
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $sac = @('off', 'on', 'evaluation')[[Math]::Min([Math]::Max((Get-SmartAppControl), 0), 2)]
    Write-Log "Windows $os, $arch, PowerShell $($PSVersionTable.PSVersion), Smart App Control $sac, folder $Root"
    if ((Get-SmartAppControl) -eq 1) {
        Fail 'Smart App Control is turned on. YardGuard needs it off.' ('Smart App Control blocks the AI libraries ' +
             'YardGuard runs on. Open Windows Security > App & browser control > Smart App Control settings, ' +
             'choose Off, then start YardGuard again. See "Smart App Control" in README.md.')
    }
    if ($os.Major -lt 10 -or $os.Build -lt 17763) {
        Fail 'YardGuard needs Windows 10 (version 1809 or newer) or Windows 11.' 'Update Windows, then try again.'
    }
    if (-not [Environment]::Is64BitOperatingSystem -or $arch -ne 'AMD64') {
        Fail "YardGuard needs 64-bit Windows on an Intel or AMD processor (this PC: $arch)." `
             'Windows on ARM and 32-bit Windows are not supported.'
    }
    $longPaths = $false
    try { $longPaths = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled -eq 1 } catch { }
    if ($Root.Length -gt 80 -and -not $longPaths) {
        Fail 'The YardGuard folder is too deep inside other folders.' ("Some AI library files have long names and " +
             "Windows limits the full path length. Move the YardGuard folder to a short path such as C:\YardGuard " +
             "or D:\YardGuard, then double-click YardGuard.cmd again. (Folder now: $Root)")
    }
    try {
        $probe = Join-Path $Cache 'write-test.tmp'
        [IO.File]::WriteAllText($probe, 'ok')
        Remove-Item -LiteralPath $probe -Force
    } catch {
        Fail 'YardGuard cannot write to its folder.' ("Move the YardGuard folder somewhere you can save files, for " +
             "example C:\YardGuard or your Documents folder, then try again. ($Root)")
    }
    try {
        $drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($Root))
        $freeGb = [Math]::Round($drive.AvailableFreeSpace / 1GB, 1)
        Write-Log "Free space on $($drive.Name): $freeGb GB"
        if ($freeGb -lt 3) {
            Fail "Not enough disk space: $freeGb GB free on $($drive.Name)." 'YardGuard needs about 8 GB. Free some space, then try again.'
        }
        if ($freeGb -lt 8) {
            $q = "Only $freeGb GB is free on $($drive.Name). YardGuard needs about 8 GB (the optional AI brain " +
                 "add-on needs about 12 GB more), and recorded alerts need space too.`n`nContinue anyway?"
            Add-Warning "Only $freeGb GB free on $($drive.Name)"
            if (-not (Ask-YesNo $q)) { Fail 'Setup stopped: not enough free disk space.' 'Free some disk space, then try again.' }
        }
    } catch [System.ArgumentException] {
        Write-Log 'Could not read the free disk space (network folder?)'
    }
    if ($Update) {
        $running = Get-RunningApp
        if ($running.Count) {
            Fail 'YardGuard is running.' ("Close YardGuard first (right-click its icon next to the clock and choose " +
                 "Quit, or close the window), then click Try again.")
        }
    }
    Set-Progress 1 'This PC is ready.'
}

function Step-Python {
    Start-Step 2
    $py = $Versions.python
    $ok = $false
    if (Test-Path -LiteralPath $BasePy) {
        if (Test-Signed $BasePy $py.signer) {
            try {
                $ErrorActionPreference = 'Continue'
                $v = & $BasePy -I -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
                $ok = ($LASTEXITCODE -eq 0 -and "$v".Trim() -eq $py.version)
            } catch { $ok = $false }
            $ErrorActionPreference = 'Stop'
        }
        if (-not $ok) { Write-Log '  runtime\python is incomplete or a different version: installing it again' }
    }
    if ($ok) {
        Write-Log "  Python $($py.version) already in runtime\python"
        Set-Progress 1 "Python $($py.version) is ready."
        return
    }
    $nupkg = Join-Path $Downloads "python.$($py.version).nupkg"
    Get-Download $py.url $nupkg $py.sha256 $py.size "Python $($py.version)"
    $staging = "$RuntimeDir.new"
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    Set-Progress 0.9 'Unpacking Python...'
    $n = Expand-Entries $nupkg 'tools/' $staging
    Write-Log "  unpacked $n files"
    $exe = Join-Path $staging 'python.exe'
    if (-not (Test-Path -LiteralPath $exe)) { Fail 'The Python package did not contain python.exe.' 'Click Try again.' }
    foreach ($f in @('python.exe', 'pythonw.exe', 'python312.dll')) {
        if (-not (Test-Signed (Join-Path $staging $f) $py.signer)) {
            Fail "Python's $f is not signed by the $($py.signer)." 'The download may have been altered. Click Try again.'
        }
    }
    if (Test-Path -LiteralPath $RuntimeDir) { Remove-Item -LiteralPath $RuntimeDir -Recurse -Force }
    Move-Item -LiteralPath $staging -Destination $RuntimeDir
    $r = Invoke-Tool $BasePy @('-I', '-c', 'import sys, venv, ensurepip; print(sys.version)')
    $v = $r.Text
    if ($r.Code -ne 0) { Fail 'The downloaded Python does not start.' "Details: $v" }
    Write-Log "  Python ready: $v"
    Remove-Item -LiteralPath $nupkg -Force -ErrorAction SilentlyContinue
    Set-Progress 1 "Python $($py.version) is ready."
}

function Test-VenvWorks {
    $ErrorActionPreference = 'Continue'   # a warning on stderr is not a failure
    if (-not (Test-Path -LiteralPath $VenvPy) -or -not (Test-Path -LiteralPath $VenvPyw)) { return $false }
    try {
        $v = & $VenvPy -I -c "import sys; print(sys.version_info[0] * 100 + sys.version_info[1])" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$v".Trim() -eq '312')
    } catch { return $false }
}

function Step-Venv {
    Start-Step 3
    if (Test-VenvWorks) {
        Write-Log '  .venv already works: keeping it'
    } else {
        if (Test-Path -LiteralPath $Venv) {
            Write-Log '  .venv is broken (moved folder or interrupted setup): creating it again'
            Remove-Item -LiteralPath $Venv -Recurse -Force
        }
        Set-Progress 0.3 'Creating .venv...'
        $r = Invoke-Tool $BasePy @('-I', '-m', 'venv', $Venv)
        if ($r.Code -ne 0 -or -not (Test-VenvWorks)) {
            Fail 'Could not create the Python environment (.venv).' "Click Try again. Details: $($r.Text)"
        }
    }
    New-Item -ItemType File -Force -Path $Pending | Out-Null
    Set-Progress 1 'The Python environment is ready.'
}

function Get-SmartAppControl {
    <# 0 off, 1 on (blocks unknown programs), 2 evaluation mode (does not block yet). #>
    try { return [int](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState }
    catch { return 0 }
}

function Get-CpuFlavour([string]$Why) {
    Write-Log "  ${Why}: processor-only PyTorch"
    return @{ Flavour = 'cpu'; Forced = $false }
}

function Get-TorchFlavour {
    $state = Read-State
    if ($Torch -ne 'auto') { return @{ Flavour = ($(if ($Torch -eq 'cuda') { 'cu128' } else { 'cpu' })); Forced = $true } }
    if ($state -and $state.torch_forced) { return @{ Flavour = $state.torch; Forced = $true } }
    $smi = if ($AssumeNoGpu) { $null } else { Find-NvidiaSmi }
    if (-not $smi) { return Get-CpuFlavour 'no NVIDIA driver found (nvidia-smi.exe)' }
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $smi '--query-gpu=name,driver_version' '--format=csv,noheader' 2>$null)
        $code = $LASTEXITCODE
        $info = @($lines | Where-Object { "$_".Trim() }) | Select-Object -First 1
        if ($code -ne 0 -or -not $info) { throw "nvidia-smi gave no answer (exit $code)" }
        Write-Log "  NVIDIA GPU: $info"
        $script:GpuFound = $true
        $driver = ("$info" -split ',')[-1].Trim()
        if ([int]($driver -split '\.')[0] -lt 570) {
            Add-Warning ("The NVIDIA driver ($driver) is older than version 570, which CUDA 12.8 needs. " +
                         'Update the driver (nvidia.com/drivers) so YardGuard can use the graphics card.')
        }
        return @{ Flavour = 'cu128'; Forced = $false }
    } catch {
        return Get-CpuFlavour "nvidia-smi did not work ($_)"
    }
}

function Step-Packages {
    Start-Step 4
    $choice = Get-TorchFlavour
    $flavour = $choice.Flavour
    $torchReqs = Join-Path $PSScriptRoot "torch-$flavour.txt"
    $what = if ($flavour -eq 'cu128') { '(about 3 GB)' } else { '(about 120 MB)' }
    Write-Log "  PyTorch build: $flavour"
    $expected = @((Get-Content -LiteralPath $Reqs) + (Get-Content -LiteralPath $torchReqs) | Where-Object { $_ -match '^[A-Za-z0-9].*==' }).Count
    $script:PipSeen = 0
    $script:PipLast = "Installing PyTorch $what and the other AI libraries..."
    $script:PipClock = [Diagnostics.Stopwatch]::StartNew()
    Set-Progress 0.01 $script:PipLast
    $onLine = {
        param($line)
        if ($line -match '^(Collecting|Requirement already satisfied:?)\s+(\S+)') {
            $script:PipSeen++
            $script:PipLast = "Checking $($Matches[2] -replace '[=<>].*$', '') ($script:PipSeen of $expected)"
        } elseif ($line -match '^Downloading\s+(?:\S*/)?([^/\s]+?)-\d.*\((.+)\)') {
            $script:PipLast = "Downloading $($Matches[1]) ($($Matches[2]))"
        } elseif ($line -match '^Installing collected packages') {
            $script:PipSeen = $expected
            $script:PipLast = 'Installing the libraries (this takes a few minutes)...'
        }
        $script:PipLineAt = $script:PipClock.Elapsed
        $frac = [Math]::Min(0.85 * $script:PipSeen / [Math]::Max($expected, 1), 0.85)
        if ($script:PipLast -like 'Installing the libraries*') { $frac = 0.9 }
        $script:PipFrac = $frac
        Set-Progress $frac $script:PipLast
    }
    $script:PipLineAt = [TimeSpan]::Zero
    $script:PipFrac = 0.01
    $onTick = {
        # Big downloads and the install print nothing for minutes: show that it is still working
        $quiet = $script:PipClock.Elapsed - $script:PipLineAt
        if ($quiet.TotalSeconds -ge 5 -and $script:PipUi -ne [int]$quiet.TotalSeconds) {
            $script:PipUi = [int]$quiet.TotalSeconds
            if ($script:Ui) { Set-Progress $script:PipFrac ('{0}   ({1:m\:ss})' -f $script:PipLast, $quiet) }
        }
    }
    $pipArgs = @('-I', '-m', 'pip', '--isolated', 'install', '--no-input', '--disable-pip-version-check',
                 '--progress-bar', 'off', '--no-warn-script-location', '--only-binary', ':all:',
                 '--cache-dir', $PipCache, '--require-hashes', '-r', $Reqs, '-r', $torchReqs)
    $r = Invoke-Tool $VenvPy $pipArgs $onLine $onTick
    if ($r.Code -ne 0) {
        $t = $r.Text
        $tail = (($t -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 6) -join "`n"
        if ($t -match 'blocked' -and $t -match 'polic') {
            Fail 'Windows blocked one of the AI library files.' ("Smart App Control or another security policy " +
                 "stopped a file. See 'Smart App Control' in README.md. $tail")
        } elseif ($t -match 'DO NOT MATCH THE HASHES') {
            Fail 'A downloaded library did not match its expected checksum.' ("Click Try again. If it keeps " +
                 "happening, a proxy or antivirus may be changing downloads. $tail")
        } elseif ($t -match 'No space left|Errno 28|not enough space') {
            Fail 'The disk is full.' 'Free some disk space, then click Try again.'
        } elseif ($t -match 'ConnectionError|Read timed out|NewConnectionError|getaddrinfo|ProxyError|SSLError') {
            Fail 'Could not download the AI libraries.' ("Check that this PC is online, then click Try again " +
                 "(finished downloads are kept). $tail")
        }
        Fail 'Installing the AI libraries failed.' "Click Try again. The log has the details. $tail"
    }
    $script:TorchChoice = $choice
    Set-Progress 1 'The AI libraries are installed.'
}

function Step-Ffmpeg {
    Start-Step 5
    $ff = $Versions.ffmpeg
    $want = @{}
    foreach ($p in $ff.files.PSObject.Properties) { $want[$p.Name] = $p.Value }
    $ok = $true
    foreach ($rel in $want.Keys) {
        $path = Join-Path $FfmpegDir ($rel -replace '/', '\')
        if (-not (Test-Path -LiteralPath $path)) { $ok = $false; break }
        if ($want[$rel] -and (Get-Sha256 $path) -ne $want[$rel]) { $ok = $false; break }
    }
    if ($ok) {
        Write-Log "  ffmpeg $($ff.version) already in tools\ffmpeg"
    } else {
        $zip = Join-Path $Downloads (Split-Path -Leaf $ff.url)
        Get-Download $ff.url $zip $ff.sha256 $ff.size "ffmpeg $($ff.version)"
        $staging = "$FfmpegDir.new"
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
        Set-Progress 0.95 'Unpacking ffmpeg...'
        $n = Expand-Entries $zip "$($ff.folder)/" $staging @($want.Keys)
        foreach ($rel in $want.Keys) {
            $path = Join-Path $staging ($rel -replace '/', '\')
            if (-not (Test-Path -LiteralPath $path)) { Fail "The ffmpeg package did not contain $rel." 'Click Try again.' }
            if ($want[$rel] -and (Get-Sha256 $path) -ne $want[$rel]) { Fail "ffmpeg's $rel failed its SHA-256 check." 'Click Try again.' }
        }
        if (Test-Path -LiteralPath $FfmpegDir) { Remove-Item -LiteralPath $FfmpegDir -Recurse -Force }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $FfmpegDir) | Out-Null
        Move-Item -LiteralPath $staging -Destination $FfmpegDir
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        Write-Log "  ffmpeg $($ff.version) installed ($n files)"
    }
    $exe = Join-Path $FfmpegDir 'bin\ffmpeg.exe'
    try {   # stdout only: with 'Stop', a blocked program throws, and stderr lines would too
        $v = & $exe -hide_banner -version | Select-Object -First 1
        Write-Log "  $v"
    } catch {
        Add-Warning ("Windows did not allow ffmpeg to run ($($_.Exception.Message)). Camera monitoring needs it: " +
                     "see 'Smart App Control' in README.md.")
    }
    Set-Progress 1 'ffmpeg is ready.'
}

function Step-Verify {
    Start-Step 6
    Set-Progress 0.2 'Starting the AI libraries once (this can take a minute)...'
    $code = 'import sys; sys.path.insert(0, sys.argv[1]); ' +
            'import numpy, cv2, PIL, PySide6.QtWidgets, PySide6.QtMultimedia, PySide6.QtSvg, torch, torchvision, ' +
            'ultralytics, paho.mqtt.client, segno, requests, psutil; ' +
            'import yardguard.gui, yardguard.engine, yardguard.accel, yardguard.video, yardguard.plates; ' +
            'print("torch", torch.__version__, "cuda", torch.cuda.is_available(), "opencv", cv2.__version__)'
    $r = Invoke-Tool $VenvPy @('-I', '-c', $code, $Root)
    if ($r.Code -ne 0) {
        $tail = (($r.Text -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 5) -join "`n"
        if ($r.Text -match 'blocked' -and $r.Text -match 'polic') {
            Fail 'Windows blocked one of the AI library files.' ("Smart App Control or another security policy " +
                 "stopped a file. See 'Smart App Control' in README.md. $tail")
        }
        Fail 'The installed libraries do not start.' "Click Try again. If that does not help, see the log. $tail"
    }
    $line = ($r.Text -split "`r?`n" | Where-Object { $_ -like 'torch *' } | Select-Object -First 1)
    Write-Log "  $line"
    if ($script:GpuFound -and $script:TorchChoice.Flavour -eq 'cu128' -and $line -notmatch 'cuda True') {
        Add-Warning ('PyTorch for the graphics card is installed, but it cannot use the GPU yet (update the NVIDIA ' +
                     'driver and restart the PC). Until then the AI runs on the processor.')
    }
    $state = [ordered]@{
        installed    = (Get-Date).ToString('s')
        python       = $Versions.python.version
        torch        = $script:TorchChoice.Flavour
        torch_forced = [bool]$script:TorchChoice.Forced
        ffmpeg       = $Versions.ffmpeg.version
        check        = "$line"
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8
    Remove-Item -LiteralPath $Pending -Force -ErrorAction SilentlyContinue
    Set-Progress 1 'Everything works.'
}

function Start-YardGuard {
    Write-Log 'Starting YardGuard'
    Start-Process -FilePath $VenvPyw -ArgumentList (ConvertTo-Arg (Join-Path $Root 'app.py')) -WorkingDirectory $Root
}

# --- Main -------------------------------------------------------------------------------------

Write-Log ('=' * 70)
Write-Log ("YardGuard setup started" + $(if ($Update) { ' (update)' } else { '' }) + " in $Root")
Start-Ui
$clock = [Diagnostics.Stopwatch]::StartNew()
$exitCode = 0
while ($true) {
    try {
        $script:Warnings.Clear()
        Step-Check
        Step-Python
        Step-Venv
        Step-Packages
        Step-Ffmpeg
        Step-Verify
        $mins = [Math]::Round($clock.Elapsed.TotalMinutes, 1)
        Write-Log "Setup finished in $mins min"
        if ($script:Ui) {
            $script:Ui.Title.Text = 'YardGuard is ready'
            $script:Ui.Step.Text = $(if ($NoLaunch) { 'Setup finished.' } else { 'Starting YardGuard...' })
            $script:Ui.Detail.Text = $(if ($script:Warnings.Count) { 'Please note: ' + ($script:Warnings -join ' ') } else { '' })
            $script:Ui.Close.Text = 'Close'
            $script:Ui.Bar.Value = 1000
            [System.Windows.Forms.Application]::DoEvents()
            Save-UiShot 'done'
            $wait = if ($script:Warnings.Count) { 12000 } else { 1500 }
            $t = [Diagnostics.Stopwatch]::StartNew()
            $script:Choice = $null
            while ($t.ElapsedMilliseconds -lt $wait -and -not $script:Choice -and $script:Ui.Form.Visible) {
                [System.Windows.Forms.Application]::DoEvents()
                Start-Sleep -Milliseconds 50
            }
        }
        if (-not $NoLaunch) { Start-YardGuard }
        $exitCode = 0
        break
    } catch {
        $message = $_.Exception.Message
        $advice = "$($_.Exception.Data['advice'])"
        if ($script:Cancelled -and $message -eq 'Setup was cancelled.') {
            Write-Log 'Setup cancelled by the user'
            $exitCode = 11
            break
        }
        Write-Log "ERROR: $message"
        if ($advice) { Write-Log "  advice: $advice" }
        if ($_.InvocationInfo) { Write-Log "  at $($_.InvocationInfo.PositionMessage -replace "`r?`n", ' ')" }
        $exitCode = 10
        if ((Show-Failure $message "$advice`n`nLog: $LogFile") -ne 'retry') { break }
        Write-Log 'Trying again'
        $clock.Restart()
    }
}
if ($script:Ui -and $script:Ui.Form.Visible) { $script:Ui.Form.Close() }
exit $exitCode
