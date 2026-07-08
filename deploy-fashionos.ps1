<#
.SYNOPSIS
    Rebuilds, pushes, and redeploys FashionOS (api + worker) to Azure Container Apps.

.DESCRIPTION
    Run this from the project root (where the Dockerfile lives) any time you
    change code shared by fashionos-api / fashionos-worker.

    Steps:
      1. docker build
      2. az acr login
      3. docker push
      4. verify the tag actually landed in ACR
      5. force a new revision on both Container Apps (unique suffix each run)
      6. wait a few seconds, then show replica health for both

.USAGE
    From D:\fashionos (or wherever the Dockerfile is):
        .\deploy-fashionos.ps1

    Optional: only redeploy one app
        .\deploy-fashionos.ps1 -Only api
        .\deploy-fashionos.ps1 -Only worker

    Optional: skip the build/push (just force a redeploy of the current 'latest' image,
    e.g. after only changing an env var)
        .\deploy-fashionos.ps1 -SkipBuild
#>

param(
    [ValidateSet("both", "api", "worker")]
    [string]$Only = "both",

    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

# -- Config - edit these if names ever change ----------------------------------
$AcrName        = "fashionosacr"
$AcrLoginServer = "fashionosacr.azurecr.io"
$ImageName      = "fashionos-app"
$ImageTag       = "latest"
$ResourceGroup  = "fashionos-rg"
$ApiAppName     = "fashionos-api"
$WorkerAppName  = "fashionos-worker"

$FullImage = "$AcrLoginServer/${ImageName}:${ImageTag}"
$RevisionSuffix = "deploy" + (Get-Date -Format "MMddHHmm")

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "    OK: $msg" -ForegroundColor Green
}

function Write-Fail($msg) {
    Write-Host "    FAILED: $msg" -ForegroundColor Red
}

# -- 1. Build ----------------------------------------------------------------
if (-not $SkipBuild) {
    Write-Step "Building image: $FullImage"
    docker build -t $FullImage .
    if ($LASTEXITCODE -ne 0) { Write-Fail "docker build failed"; exit 1 }
    Write-Ok "Image built"

    # -- 2. ACR login --------------------------------------------------------
    Write-Step "Logging in to ACR ($AcrName)"
    az acr login --name $AcrName
    if ($LASTEXITCODE -ne 0) { Write-Fail "az acr login failed"; exit 1 }
    Write-Ok "Logged in"

    # -- 3. Push -------------------------------------------------------------
    Write-Step "Pushing image to ACR"
    docker push $FullImage
    if ($LASTEXITCODE -ne 0) { Write-Fail "docker push failed"; exit 1 }
    Write-Ok "Image pushed"

    # -- 4. Verify tag actually landed ---------------------------------------
    Write-Step "Verifying tag exists in ACR"
    $tags = az acr repository show-tags --name $AcrName --repository $ImageName --output tsv
    if ($tags -notcontains $ImageTag) {
        Write-Fail "Tag '$ImageTag' not found in ACR after push. Tags present: $tags"
        exit 1
    }
    Write-Ok "Tag '$ImageTag' confirmed in ACR"
} else {
    Write-Step "Skipping build/push (-SkipBuild) - will just force a redeploy of $FullImage"
}

# -- 5. Force new revision on selected app(s) ----------------------------------
function Deploy-App($AppName) {
    Write-Step "Deploying $AppName (revision suffix: $RevisionSuffix)"
    az containerapp update `
        --name $AppName `
        --resource-group $ResourceGroup `
        --image $FullImage `
        --revision-suffix $RevisionSuffix `
        --output none
    if ($LASTEXITCODE -ne 0) { Write-Fail "$AppName update failed"; exit 1 }
    Write-Ok "$AppName updated to new revision"
}

if ($Only -eq "both" -or $Only -eq "api") {
    Deploy-App $ApiAppName
}
if ($Only -eq "both" -or $Only -eq "worker") {
    Deploy-App $WorkerAppName
}

# -- 6. Wait, then check replica health ----------------------------------------
Write-Step "Waiting 15s for replicas to come up..."
Start-Sleep -Seconds 15

function Show-Health($AppName) {
    Write-Step "Replica status: $AppName"
    az containerapp replica list --name $AppName --resource-group $ResourceGroup -o table
}

if ($Only -eq "both" -or $Only -eq "api") {
    Show-Health $ApiAppName
}
if ($Only -eq "both" -or $Only -eq "worker") {
    Show-Health $WorkerAppName
}

Write-Host ""
Write-Host "==> Done. Check the replica states above (should show Running / ready true)." -ForegroundColor Yellow
Write-Host "    For the API, also sanity check: curl -i https://fashionos-api.agreeablecliff-d4c5c7cf.centralus.azurecontainerapps.io/health" -ForegroundColor Yellow