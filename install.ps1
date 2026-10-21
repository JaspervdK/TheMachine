# The Machine on Windows (beta): runs inside WSL2 (Linux), next to OpenClaw's Gateway.
#
#   irm https://raw.githubusercontent.com/Salesflare/TheMachine/main/install.ps1 | iex
#
# It sets up WSL2 with Ubuntu (or uses the OpenClawGateway distro that OpenClaw's Windows Hub made), turns on
# systemd there, installs Git, Python, Node.js 24 and Tailscale inside it, gets The Machine and runs its own
# installer (./install.sh), which offers to install and set up OpenClaw. Rerunning it is safe: it updates.
#
# Settings (environment variables, since `irm | iex` takes no arguments):
#   THE_MACHINE_DISTRO=<name>   the WSL distro to use (default: OpenClawGateway if it exists, else Ubuntu-24.04)
#   THE_MACHINE_DRY_RUN=1       only say what it would do (also: .\install.ps1 -DryRun)
#   THE_MACHINE_NO_AUTOSTART=1  do not start WSL at Windows sign-in

function Install-TheMachine {
    param([switch]$DryRun)
    $ErrorActionPreference = 'Continue'  # native commands report through exit codes, checked below
    $env:WSL_UTF8 = '1'  # wsl.exe prints UTF-16 otherwise
    $repo = 'https://github.com/Salesflare/TheMachine.git'
    $app = '~/.openclaw/workspace/tools/personal-assistant'

    function Say([string]$text) { Write-Host $text }
    function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }

    if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
        Say 'This is for Windows. On Linux or macOS run ./install.sh in the checkout.'
        return
    }

    function Distros {
        $list = & wsl.exe --list --quiet 2>$null
        if ($LASTEXITCODE -ne 0) { return @() }
        return @($list | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
    }

    function In-Wsl([string]$user, [string]$script) {
        # The script goes in base64-encoded, with Linux line ends: no quoting trouble, and stdin stays this terminal.
        $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($script -replace "`r", '') + "`n"))
        & wsl.exe -d $distro -u $user -- bash -c "echo $encoded | base64 -d > /tmp/the-machine-step.sh && bash /tmp/the-machine-step.sh; s=`$?; rm -f /tmp/the-machine-step.sh; exit `$s"
        if ($LASTEXITCODE -ne 0) { throw "This step failed inside WSL (exit $LASTEXITCODE). See the lines above." }
    }

    Step 'Checking WSL'
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    $null = & wsl.exe --status 2>$null
    $hasWsl = $wsl -and $LASTEXITCODE -eq 0
    $have = if ($hasWsl) { Distros } else { @() }
    $distro = if ($env:THE_MACHINE_DISTRO) { $env:THE_MACHINE_DISTRO }
              elseif ($have -contains 'OpenClawGateway') { 'OpenClawGateway' }
              else { 'Ubuntu-24.04' }
    Say "WSL: $(if ($hasWsl) { 'installed' } else { 'not installed' }). Distro: $distro ($(if ($have -contains $distro) { 'there' } else { 'to install' }))."

    if ($DryRun) {
        Say ''
        Say 'Dry run: nothing is changed. A real run would:'
        if (-not $hasWsl) { Say '  - install WSL (wsl --install --no-distribution; needs Administrator), then ask you to restart Windows' }
        if ($have -notcontains $distro) { Say "  - install $distro (wsl --install -d $distro); it asks for a new Linux user name and password" }
        Say '  - turn on systemd in /etc/wsl.conf and restart the distro'
        Say '  - install git, python3, curl and dbus-x11 (apt), Node.js 24 (NodeSource) when missing, and Tailscale'
        Say '  - keep your Linux user''s services running (loginctl enable-linger)'
        Say "  - get The Machine into $app (git clone, or git pull when it is there) and run ./install.sh"
        if (-not $env:THE_MACHINE_NO_AUTOSTART) { Say '  - start WSL at Windows sign-in (a small file in your Startup folder), so The Machine keeps running' }
        return
    }

    if (-not $hasWsl) {
        $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $admin) {
            Say 'WSL is not installed. Open PowerShell as Administrator (right-click, Run as administrator) and run this command again.'
            return
        }
        & wsl.exe --install --no-distribution
        Say ''
        Say 'WSL is installed. Restart Windows, then run the same command again (no Administrator needed).'
        return
    }

    if ($have -notcontains $distro) {
        Step "Installing $distro"
        Say 'It asks for a new Linux user name and password (any; you need the password for sudo).'
        Say 'When you see a Linux prompt ($), type exit and press Enter to continue here.'
        & wsl.exe --install -d $distro
        if ((Distros) -notcontains $distro) { throw "$distro did not install. Run: wsl --install -d $distro, then this command again." }
    }
    $user = ((& wsl.exe -d $distro -- whoami) | Out-String).Trim()
    if (-not $user -or $user -eq 'root') {
        throw "$distro has no own user (it runs as root). Add one (wsl -d $distro -u root -- adduser you), set it as default in /etc/wsl.conf ([user] default=you), then run this again."
    }
    Say "Linux user: $user"

    Step 'Turning on systemd inside WSL'
    In-Wsl 'root' @'
set -e
if ! grep -qs '^[[:space:]]*systemd[[:space:]]*=[[:space:]]*true' /etc/wsl.conf; then
  if grep -qs '^\[boot\]' /etc/wsl.conf; then sed -i 's/^\[boot\]/[boot]\nsystemd=true/' /etc/wsl.conf
  else printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf; fi
  echo 'systemd turned on'
else
  echo 'systemd already on'
fi
'@
    if (((& wsl.exe -d $distro -- ps -p 1 -o comm=) | Out-String).Trim() -ne 'systemd') {
        & wsl.exe --terminate $distro | Out-Null
    }
    $init = ((& wsl.exe -d $distro -- ps -p 1 -o comm=) | Out-String).Trim()
    if ($init -ne 'systemd') { throw "systemd did not start in $distro (PID 1 is $init). Update WSL (wsl --update) and run this again." }
    Say 'systemd is running'

    Step 'Installing Git, Python, Node.js and Tailscale inside WSL (a few minutes the first time)'
    In-Wsl 'root' (@'
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 curl ca-certificates dbus-x11 >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo 'Python 3.12 or newer is needed; use Ubuntu 24.04 or newer'; exit 1; }
if ! node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit((a==24&&b>=16)||a>26||(a==26&&b>=1)?0:1)' 2>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
echo "node $(node --version), $(python3 --version)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
systemctl enable --now tailscaled >/dev/null 2>&1 || true
loginctl enable-linger '__USER__'
if ! tailscale status >/dev/null 2>&1; then
  echo 'Sign this Linux machine in to Tailscale (the same tailnet as your phone): open the link below.'
  tailscale up --operator='__USER__'
fi
tailscale set --operator='__USER__' >/dev/null 2>&1 || true
echo "tailscale: $(tailscale ip -4 2>/dev/null | head -1)"
'@).Replace('__USER__', $user)

    Step 'Getting The Machine'
    In-Wsl $user @"
set -e
if [ -d $app/.git ]; then git -C $app pull --ff-only; else mkdir -p ~/.openclaw/workspace/tools && git clone $repo $app; fi
git -C $app log -1 --format='The Machine %h (%cd)' --date=short
"@

    if (-not $env:THE_MACHINE_NO_AUTOSTART) {
        Step 'Starting WSL at Windows sign-in'
        $startup = [Environment]::GetFolderPath('Startup')
        $file = Join-Path $startup 'The Machine (WSL).vbs'
        # Keeps the distro, and so its services, running after sign-in; dbus-launch avoids WSL's idle shutdown.
        Set-Content -Path $file -Encoding ASCII -Value "CreateObject(""WScript.Shell"").Run ""wsl.exe -d $distro --exec dbus-launch true"", 0, False"
        Start-Process -WindowStyle Hidden wsl.exe -ArgumentList '-d', $distro, '--exec', 'dbus-launch', 'true'
        Say "Added $file"
    }

    Step 'Running The Machine installer inside WSL'
    & wsl.exe -d $distro --cd '~' -- bash -lc "cd $app && ./install.sh"
    $code = $LASTEXITCODE
    Say ''
    Say "Done (installer exit $code). Use The Machine's commands through WSL, for example:"
    Say "  wsl -d $distro -- bash -lc 'assistant doctor'"
    Say "  wsl -d $distro -- bash -lc 'assistant support-bundle'   (logs without secrets, saved on your Desktop)"
}

Install-TheMachine -DryRun:(($env:THE_MACHINE_DRY_RUN -eq '1') -or ($args -contains '-DryRun'))
