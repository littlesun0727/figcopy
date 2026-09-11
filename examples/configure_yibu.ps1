# Configure Figcopy's Yibu providers without storing or printing an API key.
param(
    [string]$SharedPath = "D:\codes\creative-video-editor\shared.py",
    [string]$AuditBaseUrl = "http://127.0.0.1:17860",
    [string]$CredentialsFile
)

if ($CredentialsFile) {
    $resolvedCredential = Resolve-Path -LiteralPath $CredentialsFile -ErrorAction Stop
} else {
    $resolvedShared = Resolve-Path -LiteralPath $SharedPath -ErrorAction Stop
}
$healthUrl = $AuditBaseUrl.TrimEnd("/") + "/_yibu_audit/health"
$health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5 -ErrorAction Stop

if ($health.status -ne "ok" -or $health.upstream -ne "https://yibuapi.com") {
    throw "Unexpected Yibu audit proxy health: $($health | ConvertTo-Json -Compress)"
}

if ($CredentialsFile) {
    $env:YIBU_CREDENTIALS_FILE = $resolvedCredential.Path
} else {
    Remove-Item Env:YIBU_CREDENTIALS_FILE -ErrorAction SilentlyContinue
    $env:YIBU_SHARED_PATH = $resolvedShared.Path
}
$env:YIBU_AUDIT_BASE_URL = $AuditBaseUrl.TrimEnd("/")
$env:YIBU_VLM_MODEL = "kimi-k3"
$env:YIBU_VLM_MAX_TOKENS = "16384"
$env:YIBU_VLM_REASONING_EFFORT = "high"
$env:YIBU_IMAGE_MODEL = "doubao-seedream-5-0-260128"
$env:YIBU_IMAGE_SIZE = "2K"
$env:YIBU_TIMEOUT_SECONDS = "900"

Write-Host "Yibu providers configured (API key not printed)"
Write-Host "  Audit: $env:YIBU_AUDIT_BASE_URL"
Write-Host "  VLM:   $env:YIBU_VLM_MODEL (max tokens: $env:YIBU_VLM_MAX_TOKENS, reasoning: $env:YIBU_VLM_REASONING_EFFORT)"
Write-Host "  Image: $env:YIBU_IMAGE_MODEL ($env:YIBU_IMAGE_SIZE)"
