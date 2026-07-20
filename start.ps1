[CmdletBinding()]
param(
    [switch]$Lan,
    [switch]$Stop
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RegistrationUrl = "https://qveris.ai/?ref=afAfj_c90cnWYg"
$InviteCode = "75gxF1vtvXWj_A"
$ImageName = "qveris-account-proxy:local"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$OriginalEnvironment = @{}
$ApiKey = $null
$BootstrapTicket = $null
$ExitCode = 0

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$Quiet
    )

    if ($Quiet) {
        & docker @Arguments *> $null
    }
    else {
        & docker @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed."
    }
}

function Invoke-DockerWithInput {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$InputValue
    )

    try {
        $InputValue | & docker @Arguments *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Docker input command failed."
        }
    }
    finally {
        $InputValue = $null
    }
}

function Try-OpenUrl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url
    )

    try {
        $process = Start-Process -FilePath $Url -PassThru -ErrorAction Stop
        return ($null -ne $process)
    }
    catch {
        return $false
    }
}

function Resolve-BindAddress {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $candidate = $Value.Trim()
    if ($candidate.StartsWith("[") -and $candidate.EndsWith("]")) {
        $candidate = $candidate.Substring(1, $candidate.Length - 2)
    }

    $parsedAddress = $null
    if ([string]::IsNullOrWhiteSpace($candidate) -or
        -not [System.Net.IPAddress]::TryParse($candidate, [ref]$parsedAddress)) {
        throw "QVP_BIND_ADDRESS is invalid."
    }

    $isIpv6 = $parsedAddress.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6
    $canonical = $parsedAddress.ToString()
    $composeValue = if ($isIpv6) { "[{0}]" -f $canonical } else { $canonical }
    if ($canonical -eq "0.0.0.0") {
        $browserHost = "127.0.0.1"
    }
    elseif ($canonical -eq "::") {
        $browserHost = "[::1]"
    }
    elseif ($isIpv6) {
        $browserHost = "[{0}]" -f $canonical
    }
    else {
        $browserHost = $canonical
    }

    return [pscustomobject]@{
        ComposeValue = $composeValue
        BrowserHost = $browserHost
    }
}

function Resolve-LanHost {
    param(
        [string]$Override
    )

    if (-not [string]::IsNullOrWhiteSpace($Override)) {
        $parsedAddress = $null
        $candidate = $Override.Trim()
        if (-not [System.Net.IPAddress]::TryParse($candidate, [ref]$parsedAddress) -or
            $parsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
            [System.Net.IPAddress]::IsLoopback($parsedAddress) -or
            $candidate -eq "0.0.0.0" -or
            $candidate.StartsWith("169.254.")) {
            throw "QVP_LAN_HOST 必须是可供其他局域网设备访问的 IPv4 地址。"
        }
        return $parsedAddress.ToString()
    }

    try {
        foreach ($networkInterface in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            if ($networkInterface.OperationalStatus -ne [System.Net.NetworkInformation.OperationalStatus]::Up -or
                $networkInterface.NetworkInterfaceType -eq [System.Net.NetworkInformation.NetworkInterfaceType]::Loopback) {
                continue
            }
            $properties = $networkInterface.GetIPProperties()
            $hasIpv4Gateway = @($properties.GatewayAddresses | Where-Object {
                $null -ne $_.Address -and
                $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
                $_.Address.ToString() -ne "0.0.0.0"
            }).Count -gt 0
            if (-not $hasIpv4Gateway) {
                continue
            }
            foreach ($unicast in $properties.UnicastAddresses) {
                if ($unicast.Address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
                    continue
                }
                $candidate = $unicast.Address.ToString()
                if (-not [System.Net.IPAddress]::IsLoopback($unicast.Address) -and
                    $candidate -ne "0.0.0.0" -and
                    -not $candidate.StartsWith("169.254.")) {
                    return $candidate
                }
            }
        }
    }
    catch {
        # Fall through to an explicit placeholder when adapter discovery is unavailable.
    }

    return "LAN_IP"
}

$InitializeVolumesScript = @'
import os
import re
import secrets
import stat

UID = 10001
GID = 10001
ROOTS = ("/config", "/run/secrets", "/run/account-secrets")
STALE = {
    "/config": re.compile(r"\.accounts\.json\.qvp-tmp-[0-9a-f]{32}"),
    "/run/secrets": re.compile(r"\.proxy_access_token\.qvp-tmp-[0-9a-f]{32}"),
}


def require_directory(path):
    mode = os.lstat(path).st_mode
    if not stat.S_ISDIR(mode):
        raise RuntimeError("volume root is not a directory")


def require_regular(path):
    mode = os.lstat(path).st_mode
    if not stat.S_ISREG(mode):
        raise RuntimeError("managed path is not a regular file")


def secure_tree(root):
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        require_directory(current)
        os.chown(current, UID, GID)
        os.chmod(current, 0o700)
        for name in directories:
            require_directory(os.path.join(current, name))
        for name in files:
            path = os.path.join(current, name)
            require_regular(path)
            os.chown(path, UID, GID)
            os.chmod(path, 0o600)


def remove_stale(root, pattern):
    for entry in os.scandir(root):
        if not pattern.fullmatch(entry.name):
            continue
        mode = entry.stat(follow_symlinks=False).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise RuntimeError("stale path has an unexpected type")
        os.unlink(entry.path)


def atomic_create(path, payload):
    directory = os.path.dirname(path)
    name = os.path.basename(path)
    temporary = os.path.join(
        directory, f".{name}.qvp-tmp-{secrets.token_hex(16)}"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchown(descriptor, UID, GID)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass
    finally:
        os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


for root in ROOTS:
    require_directory(root)
for root, pattern in STALE.items():
    remove_stale(root, pattern)
for root in ROOTS:
    secure_tree(root)

accounts_path = "/config/accounts.json"
accounts_missing = not os.path.lexists(accounts_path)
if not accounts_missing:
    require_regular(accounts_path)

token_path = "/run/secrets/proxy_access_token"
if not os.path.lexists(token_path):
    token = f"sk-{secrets.token_urlsafe(32)}"
    atomic_create(token_path, token.encode("ascii"))
require_regular(token_path)
os.chown(token_path, UID, GID)
os.chmod(token_path, 0o600)

print("accounts-missing" if accounts_missing else "accounts-present")
'@

$InitializeAccountsScript = @'
import json
import os
import re
import secrets
import stat
import sys

UID = 10001
GID = 10001
path = "/config/accounts.json"

if os.path.lexists(path):
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise RuntimeError("accounts path is not a regular file")
    raise SystemExit(0)

raw = sys.stdin.buffer.read(4097).rstrip(b"\r\n")
try:
    api_key = raw.decode("ascii")
except UnicodeDecodeError as error:
    raise RuntimeError("invalid API key") from error
if not re.fullmatch(r"[A-Za-z0-9._-]{8,4096}", api_key):
    raise RuntimeError("invalid API key")

profile_id = secrets.token_hex(16)
accept_language = secrets.choice(
    (
        "zh-CN,zh;q=0.9,en;q=0.8",
        "zh-CN,zh;q=0.9",
        "en-US,en;q=0.9,zh-CN;q=0.8",
    )
)
document = {
    "accounts": [
        {
            "id": "account-a",
            "name": "账号 1",
            "weight": 1,
            "requests_per_minute": 10,
            "burst": 10,
            "transport": {
                "user_agent": f"qveris-account-proxy/0.1.0 profile/{profile_id}",
                "accept_language": accept_language,
            },
            "keys": [{"id": "primary", "api_key": api_key}],
            "oauth_tokens": [],
        }
    ]
}
payload = (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
api_key = ""
raw = b""

directory = os.path.dirname(path)
temporary = os.path.join(
    directory, f".accounts.json.qvp-tmp-{secrets.token_hex(16)}"
)
descriptor = os.open(
    temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    os.fchown(descriptor, UID, GID)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb", closefd=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError:
        pass
finally:
    os.close(descriptor)
    if os.path.lexists(temporary):
        os.unlink(temporary)

if not stat.S_ISREG(os.lstat(path).st_mode):
    raise RuntimeError("accounts path is not a regular file")
os.chown(path, UID, GID)
os.chmod(path, 0o600)
'@

$BootstrapTicketScript = @'
import json
import re
import urllib.request

with open("/run/secrets/proxy_access_token", "r", encoding="ascii") as stream:
    token = stream.read().strip()
request = urllib.request.Request(
    "http://127.0.0.1:8080/admin/v1/bootstrap-ticket",
    data=b"",
    method="POST",
    headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    },
)
token = ""
with urllib.request.urlopen(request, timeout=3) as response:
    ticket = json.load(response).get("ticket", "")
if not isinstance(ticket, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket) is None:
    raise RuntimeError("invalid bootstrap ticket")
print(ticket)
'@

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is not installed."
    }
    Invoke-Docker -Arguments @("version") -Quiet
    Invoke-Docker -Arguments @("compose", "version") -Quiet

    $ProjectName = if ([string]::IsNullOrWhiteSpace($env:QVP_PROJECT_NAME)) {
        "qveris-proxy"
    }
    else {
        $env:QVP_PROJECT_NAME.Trim()
    }
    if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]{0,62}$') {
        throw "QVP_PROJECT_NAME is invalid."
    }

    $portText = if ([string]::IsNullOrWhiteSpace($env:QVP_HOST_PORT)) {
        "18081"
    }
    else {
        $env:QVP_HOST_PORT.Trim()
    }
    $port = 0
    if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw "QVP_HOST_PORT is invalid."
    }

    $bindInput = if ($Lan) {
        "0.0.0.0"
    }
    elseif ([string]::IsNullOrWhiteSpace($env:QVP_BIND_ADDRESS)) {
        "127.0.0.1"
    }
    else {
        $env:QVP_BIND_ADDRESS.Trim()
    }
    $resolvedBind = Resolve-BindAddress -Value $bindInput
    $defaultAccount = if ([string]::IsNullOrWhiteSpace($env:QVP_DEFAULT_ACCOUNT)) {
        ""
    }
    else {
        $env:QVP_DEFAULT_ACCOUNT.Trim()
    }
    $routingMode = if ([string]::IsNullOrWhiteSpace($env:QVP_ROUTING_MODE)) {
        "round_robin"
    }
    else {
        $env:QVP_ROUTING_MODE.Trim()
    }
    if ($routingMode -notin @("round_robin", "explicit")) {
        throw "QVP_ROUTING_MODE 必须是 round_robin 或 explicit。"
    }

    $environmentUpdates = [ordered]@{
        QVP_SECRET_DIR = $Root
        QVP_ACCOUNT_SECRETS_DIR = $Root
        QVP_CONFIG_DIR = $Root
        QVP_BIND_ADDRESS = $resolvedBind.ComposeValue
        QVP_HOST_PORT = [string]$port
        QVP_DEFAULT_ACCOUNT = $defaultAccount
        QVP_ROUTING_MODE = $routingMode
        QVP_ALLOW_API_KEY_FOR_OAUTH_ROUTES = "true"
    }
    foreach ($name in $environmentUpdates.Keys) {
        $OriginalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, [string]$environmentUpdates[$name], "Process")
    }

    $composeArguments = @(
        "compose",
        "-p", $ProjectName,
        "-f", (Join-Path $Root "compose.yaml"),
        "-f", (Join-Path $Root "compose.lite.yaml"),
        "-f", (Join-Path $Root "compose.ui.yaml"),
        "-f", (Join-Path $Root "compose.quickstart.yaml")
    )

    if ($Stop) {
        Write-Host "正在停止 QVeris Proxy..."
        Invoke-Docker -Arguments ($composeArguments + @("down", "--remove-orphans"))
        Write-Host "QVeris Proxy 已停止，Docker 卷中的配置和状态会保留。" -ForegroundColor Green
        return
    }

    Write-Host "正在构建 QVeris Proxy 镜像..."
    Invoke-Docker -Arguments ($composeArguments + @("build", "proxy"))

    $configVolume = "{0}_qveris_config" -f $ProjectName
    $secretsVolume = "{0}_qveris_secrets" -f $ProjectName
    $accountSecretsVolume = "{0}_qveris_account_secrets" -f $ProjectName
    $volumes = [ordered]@{
        qveris_config = $configVolume
        qveris_secrets = $secretsVolume
        qveris_account_secrets = $accountSecretsVolume
    }
    foreach ($volumeKey in $volumes.Keys) {
        Invoke-Docker -Arguments @(
            "volume", "create",
            "--label", ("com.docker.compose.project={0}" -f $ProjectName),
            "--label", ("com.docker.compose.volume={0}" -f $volumeKey),
            "--label", "io.github.hulwe.qveris.quickstart=1",
            $volumes[$volumeKey]
        ) -Quiet

        $labelArguments = @(
            "volume", "inspect", "--format",
            '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}|{{index .Labels "io.github.hulwe.qveris.quickstart"}}',
            $volumes[$volumeKey]
        )
        $labelOutput = @(& docker @labelArguments 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw "The named volume ownership could not be verified."
        }
        $actualLabels = (($labelOutput -join "").Trim())
        $labelOutput = $null
        $expectedLabels = "{0}|{1}|1" -f $ProjectName, $volumeKey
        if ($actualLabels -ne $expectedLabels) {
            throw "A named volume already belongs to another project. Set a different QVP_PROJECT_NAME."
        }
    }

    $volumeMounts = @(
        "--mount", ("type=volume,source={0},target=/config" -f $configVolume),
        "--mount", ("type=volume,source={0},target=/run/secrets" -f $secretsVolume),
        "--mount", ("type=volume,source={0},target=/run/account-secrets" -f $accountSecretsVolume)
    )
    $initializeArguments = @(
        "run", "--rm", "--user", "0:0"
    ) + $volumeMounts + @(
        "--entrypoint", "python", $ImageName, "-c", $InitializeVolumesScript
    )
    $initializeOutput = @(& docker @initializeArguments 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "The named volumes could not be initialized."
    }
    $accountState = (($initializeOutput -join "").Trim())
    $initializeOutput = $null
    if ($accountState -notin @("accounts-missing", "accounts-present")) {
        throw "The named volume state is invalid."
    }

    if ($accountState -eq "accounts-missing") {
        Write-Host "首次配置 QVeris Proxy"
        Write-Host ("注册链接：{0}" -f $RegistrationUrl)
        Write-Host ("邀请码：{0}" -f $InviteCode)
        if (-not (Try-OpenUrl -Url $RegistrationUrl)) {
            Write-Host "浏览器未自动打开，请手动访问上面的注册链接。"
        }

        while ($true) {
            $secureApiKey = Read-Host "粘贴 QVeris API Key（输入不会显示）" -AsSecureString
            $bstr = [IntPtr]::Zero
            try {
                $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApiKey)
                $ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
            }
            finally {
                if ($bstr -ne [IntPtr]::Zero) {
                    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
                }
                $secureApiKey.Dispose()
            }

            if ($ApiKey.Length -ge 8 -and $ApiKey.Length -le 4096 -and
                $ApiKey -match '^[A-Za-z0-9._-]+$') {
                break
            }
            $ApiKey = $null
            Write-Host "API Key 格式无效，请重新输入。" -ForegroundColor Yellow
        }

        $accountArguments = @(
            "run", "--rm", "-i", "--user", "0:0",
            "--mount", ("type=volume,source={0},target=/config" -f $configVolume),
            "--entrypoint", "python", $ImageName, "-c", $InitializeAccountsScript
        )
        Invoke-DockerWithInput -Arguments $accountArguments -InputValue $ApiKey
        $ApiKey = $null
    }

    $verificationArguments = @(
        "run", "--rm", "--user", "10001:10001"
    ) + $volumeMounts + @(
        "--entrypoint", "sh", $ImageName, "-c",
        "test -r /config/accounts.json && test -w /config && test -r /run/secrets/proxy_access_token && test -x /run/account-secrets"
    )
    Invoke-Docker -Arguments $verificationArguments -Quiet

    Write-Host "正在启动轻量可视化服务..."
    Invoke-Docker -Arguments ($composeArguments + @("up", "-d", "proxy"))

    $containerArguments = $composeArguments + @("ps", "-q", "proxy")
    $containerOutput = @(& docker @containerArguments)
    if ($LASTEXITCODE -ne 0) {
        throw "The proxy container could not be located."
    }
    $containerId = [string]($containerOutput | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($containerId)) {
        throw "The proxy container could not be located."
    }
    $containerId = $containerId.Trim()

    Write-Host "正在等待服务就绪..."
    $healthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $healthOutput = @(& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $containerId 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $health = ([string]($healthOutput | Select-Object -First 1)).Trim()
            if ($health -eq "healthy") {
                $healthy = $true
                break
            }
            if ($health -in @("exited", "dead")) {
                break
            }
        }
        Start-Sleep -Seconds 2
    }
    if (-not $healthy) {
        throw "The proxy did not become ready in time."
    }

    $adminUrl = "http://{0}:{1}/admin/" -f $resolvedBind.BrowserHost, $port
    $apiHost = if ($Lan) { Resolve-LanHost -Override $env:QVP_LAN_HOST } else { $resolvedBind.BrowserHost }
    $baseUrl = "http://{0}:{1}/api/v1" -f $apiHost, $port
    $ticketOutput = @(& docker exec --user "10001:10001" $containerId python -c $BootstrapTicketScript 2>$null)
    if ($LASTEXITCODE -eq 0) {
        $BootstrapTicket = (($ticketOutput -join "").Trim())
    }
    $ticketOutput = $null
    $autoConnect = $BootstrapTicket -match '^[A-Za-z0-9_-]{43}$'
    $launchId = [guid]::NewGuid().ToString("N")
    $launchUrl = if ($autoConnect) {
        "{0}?launch={1}#bootstrap_ticket={2}" -f $adminUrl, $launchId, [Uri]::EscapeDataString($BootstrapTicket)
    }
    else {
        "{0}?launch={1}" -f $adminUrl, $launchId
    }
    $opened = Try-OpenUrl -Url $launchUrl
    $BootstrapTicket = $null
    $launchId = $null
    $launchUrl = $null

    Write-Host "QVeris Proxy 已就绪。" -ForegroundColor Green
    Write-Host ("管理页：{0}" -f $adminUrl)
    Write-Host ("API Base URL：{0}" -f $baseUrl)
    if ($Lan -and $apiHost -eq "LAN_IP") {
        Write-Host "未自动识别局域网地址：请把 LAN_IP 换成这台电脑的 IPv4 地址，或设置 QVP_LAN_HOST 后重启。" -ForegroundColor Yellow
    }
    if ($autoConnect) {
        Write-Host "管理页已自动连接；可在“运行状态”的“接入应用”区域显示或复制代理 API Key。"
    }
    else {
        Write-Host "自动连接链接生成失败。请运行下面的命令显示管理登录令牌，再在管理页展开“手动连接”：" -ForegroundColor Yellow
        Write-Host ("docker run --rm --user 10001:10001 --mount type=volume,source={0},target=/run/secrets --entrypoint cat {1} /run/secrets/proxy_access_token" -f $secretsVolume, $ImageName)
    }
    if (-not $opened) {
        Write-Host "浏览器未自动打开，请手动访问管理页；重新运行启动脚本会再次尝试自动连接。"
    }
}
catch {
    $ExitCode = 1
    Write-Host "启动失败。请确认 Docker 正常运行，并检查容器状态。" -ForegroundColor Red
    Write-Host ("原因：{0}" -f $_.Exception.Message) -ForegroundColor Red
}
finally {
    $ApiKey = $null
    $BootstrapTicket = $null
    $BootstrapTicketScript = $null
    foreach ($name in $OriginalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $OriginalEnvironment[$name], "Process")
    }
}

if ($ExitCode -ne 0) {
    exit $ExitCode
}
