param(
    [switch]$Public,
    [switch]$Private,
    [string]$Only = "",
    [int]$DelaySeconds = 300,
    [int]$RateLimitDelaySeconds = 300,
    [int]$MaxRetries = 6
)

$ErrorActionPreference = "Stop"

if ($Public -and $Private) {
    throw "Use either -Public or -Private, not both."
}
$Visibility = if ($Private) { "2" } else { "0" }
$VisibilityArgs = @("--visibility", $Visibility)

$SteamUser = "jappoman"
$SteamCmd = Join-Path $PSScriptRoot "tools\steamcmd\steamcmd.exe"
$WorkshopDir = Join-Path $PSScriptRoot "wallpaper_engine_build\workshop"
$VdfDir = Join-Path $WorkshopDir "vdf"
$ConsoleLog = Join-Path $PSScriptRoot "tools\steamcmd\logs\console_log.txt"
$UploadStatePath = Join-Path $WorkshopDir "upload-state.json"
$DeployReportPath = Join-Path $WorkshopDir "deployment-report.json"

function Get-VdfField {
    param(
        [string]$VdfPath,
        [string]$Field
    )

    $Match = Select-String -LiteralPath $VdfPath -Pattern "`"$Field`"\s+`"([^`"]*)`"" | Select-Object -First 1
    if ($Match) {
        return $Match.Matches[0].Groups[1].Value
    }

    return ""
}

function Get-UploadSignature {
    param([string]$VdfPath)

    $Sha = [System.Security.Cryptography.SHA256]::Create()
    $Builder = [System.Text.StringBuilder]::new()

    $VdfText = Get-Content -LiteralPath $VdfPath -Raw
    $VdfText = $VdfText -replace '"publishedfileid"\s+"[^"]*"', '"publishedfileid" "0"'
    [void]$Builder.AppendLine($VdfText)

    $ContentFolder = Get-VdfField $VdfPath "contentfolder"
    $PreviewFile = Get-VdfField $VdfPath "previewfile"
    $Files = @()

    if ($ContentFolder -and (Test-Path -LiteralPath $ContentFolder)) {
        $Files += Get-ChildItem -LiteralPath $ContentFolder -File -Recurse | Sort-Object FullName
    }
    if ($PreviewFile -and (Test-Path -LiteralPath $PreviewFile)) {
        $Files += Get-Item -LiteralPath $PreviewFile
    }

    foreach ($File in $Files) {
        $Relative = $File.FullName
        if ($ContentFolder -and $File.FullName.StartsWith($ContentFolder)) {
            $Relative = $File.FullName.Substring($ContentFolder.Length)
        }
        [void]$Builder.AppendLine($Relative)
        [void]$Builder.AppendLine((Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash)
    }

    $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Builder.ToString())
    return ([BitConverter]::ToString($Sha.ComputeHash($Bytes)) -replace "-", "").ToLowerInvariant()
}

function Read-UploadState {
    $State = @{}
    if (-not (Test-Path -LiteralPath $UploadStatePath)) {
        return $State
    }

    $Json = Get-Content -LiteralPath $UploadStatePath -Raw | ConvertFrom-Json
    foreach ($Property in $Json.PSObject.Properties) {
        $State[$Property.Name] = $Property.Value.signature
    }
    return $State
}

function Write-UploadState {
    param([hashtable]$State)

    $Out = [ordered]@{}
    foreach ($Name in ($State.Keys | Sort-Object)) {
        $Out[$Name] = [ordered]@{ signature = $State[$Name] }
    }
    $Out | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $UploadStatePath -Encoding UTF8
}

function Write-DeploymentReport {
    param(
        [object[]]$Items,
        [hashtable]$State,
        [hashtable]$RunResults = @{}
    )

    $ReportItems = foreach ($Item in $Items) {
        $PublishedFileId = Get-VdfField $Item.FullName "publishedfileid"
        $Signature = Get-UploadSignature $Item.FullName
        $PreviousSignature = $State[$Item.BaseName]
        if (-not $PublishedFileId -or $PublishedFileId -eq "0") {
            $Status = "pending-create"
        } elseif ($PreviousSignature -eq $Signature) {
            $Status = "synced"
        } else {
            $Status = "needs-upload"
        }
        $ReportItem = [ordered]@{
            slug = $Item.BaseName
            publishedfileid = if ($PublishedFileId) { $PublishedFileId } else { "0" }
            signature = $Signature
            status = $Status
        }

        if ($RunResults.ContainsKey($Item.BaseName)) {
            $Result = $RunResults[$Item.BaseName]
            $ReportItem.last_run = [ordered]@{
                status = $Result.Status
                attempts = $Result.Attempts
                message = $Result.Message
                completed_at = $Result.CompletedAt
            }
        }

        $ReportItem
    }

    [ordered]@{
        generated_at = (Get-Date).ToUniversalTime().ToString("o")
        items = @($ReportItems)
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $DeployReportPath -Encoding UTF8
}

function Invoke-WorkshopUpload {
    param([string]$VdfPath)

    $Name = Split-Path -Leaf $VdfPath
    for ($Attempt = 1; $Attempt -le $MaxRetries; $Attempt++) {
        Write-Host "Uploading/updating $Name (attempt $Attempt/$MaxRetries)..."
        $BeforeLog = ""
        if (Test-Path -LiteralPath $ConsoleLog) {
            $BeforeLog = Get-Content -LiteralPath $ConsoleLog -Raw
        }

        $SteamOutput = & $SteamCmd +login $SteamUser +workshop_build_item $VdfPath +quit 2>&1
        $ExitCode = $LASTEXITCODE
        $SteamOutput | ForEach-Object { Write-Host $_ }

        $AttemptText = ""
        if (Test-Path -LiteralPath $ConsoleLog) {
            $AfterLog = Get-Content -LiteralPath $ConsoleLog -Raw
            if ($AfterLog.StartsWith($BeforeLog)) {
                $AttemptText = $AfterLog.Substring($BeforeLog.Length)
            } else {
                $AttemptText = $AfterLog
            }
        }

        if ($AttemptText -match "Access Denied") {
            return [pscustomobject]@{
                Status = "AccessDenied"
                Attempts = $Attempt
                Message = "Steam denied the update; it may still be under automated review."
            }
        }

        if ($ExitCode -eq 0) {
            return [pscustomobject]@{
                Status = "Success"
                Attempts = $Attempt
                Message = "SteamCMD completed successfully."
            }
        }

        if ($AttemptText -match "Rate Limit Exceeded" -and $Attempt -lt $MaxRetries) {
            $WaitSeconds = [Math]::Max($RateLimitDelaySeconds, 300)
            Write-Host "Steam rate limit reached. Waiting $WaitSeconds seconds before retrying..."
            Start-Sleep -Seconds $WaitSeconds
            continue
        }

        if ($AttemptText -match "Rate Limit Exceeded") {
            return [pscustomobject]@{
                Status = "RateLimitExceeded"
                Attempts = $Attempt
                Message = "Steam rate limit persisted after $MaxRetries attempts."
            }
        }

        return [pscustomobject]@{
            Status = "Failed"
            Attempts = $Attempt
            Message = "SteamCMD exited with code $ExitCode."
        }
    }
}

python wallpaper_engine_pipeline.py @VisibilityArgs run
if ($LASTEXITCODE -ne 0) {
    throw "Pipeline staging failed."
}

if (-not (Test-Path -LiteralPath $SteamCmd)) {
    throw "SteamCMD not found: $SteamCmd"
}

$UploadState = Read-UploadState
$RunResults = @{}
$SuccessCount = 0
$SkippedCount = 0
$PendingCount = 0
$FailedCount = 0
$Vdfs = Get-ChildItem -LiteralPath $VdfDir -Filter "*.vdf" | Sort-Object Name
if ($Only) {
    $Vdfs = $Vdfs | Where-Object { $_.BaseName -eq $Only }
    if (-not $Vdfs) {
        throw "Wallpaper VDF not found: $(Join-Path $VdfDir "$Only.vdf")"
    }
}

Write-DeploymentReport -Items $Vdfs -State $UploadState -RunResults $RunResults

foreach ($Vdf in $Vdfs) {
    $PublishedFileId = Get-VdfField $Vdf.FullName "publishedfileid"
    $Signature = Get-UploadSignature $Vdf.FullName
    $PreviousSignature = $UploadState[$Vdf.BaseName]

    if ($PublishedFileId -and $PublishedFileId -ne "0" -and $PreviousSignature -eq $Signature) {
        $RunResults[$Vdf.BaseName] = [pscustomobject]@{
            Status = "Skipped"
            Attempts = 0
            Message = "Unchanged; already synchronized."
            CompletedAt = (Get-Date).ToUniversalTime().ToString("o")
        }
        $SkippedCount++
        Write-Host "[SKIP] $($Vdf.Name): unchanged (Workshop ID $PublishedFileId)"
        Write-DeploymentReport -Items $Vdfs -State $UploadState -RunResults $RunResults
        continue
    }

    $UploadResult = Invoke-WorkshopUpload $Vdf.FullName
    if ($UploadResult.Status -eq "AccessDenied") {
        $RunResults[$Vdf.BaseName] = [pscustomobject]@{
            Status = "PendingReview"
            Attempts = $UploadResult.Attempts
            Message = $UploadResult.Message
            CompletedAt = (Get-Date).ToUniversalTime().ToString("o")
        }
        $PendingCount++
        if ($PublishedFileId -and $PublishedFileId -ne "0") {
            Write-Host "[PENDING/REVIEW] $($Vdf.Name): update denied after $($UploadResult.Attempts) attempt(s); will retry next run."
            Write-DeploymentReport -Items $Vdfs -State $UploadState -RunResults $RunResults
            continue
        }
        Write-Host "[PENDING/REVIEW] $($Vdf.Name): creation denied after $($UploadResult.Attempts) attempt(s); will retry next run."
        Write-DeploymentReport -Items $Vdfs -State $UploadState -RunResults $RunResults
        continue
    }

    if ($UploadResult.Status -ne "Success") {
        $RunResults[$Vdf.BaseName] = [pscustomobject]@{
            Status = $UploadResult.Status
            Attempts = $UploadResult.Attempts
            Message = $UploadResult.Message
            CompletedAt = (Get-Date).ToUniversalTime().ToString("o")
        }
        $FailedCount++
        Write-Host "[FAILED] $($Vdf.Name): $($UploadResult.Message)"
        Write-DeploymentReport -Items $Vdfs -State $UploadState -RunResults $RunResults
        continue
    }

    $UploadState[$Vdf.BaseName] = Get-UploadSignature $Vdf.FullName
    $RunResults[$Vdf.BaseName] = [pscustomobject]@{
        Status = "Success"
        Attempts = $UploadResult.Attempts
        Message = $UploadResult.Message
        CompletedAt = (Get-Date).ToUniversalTime().ToString("o")
    }
    $SuccessCount++
    Write-Host "[SUCCESS] $($Vdf.Name): upload/update completed after $($UploadResult.Attempts) attempt(s). Workshop ID $((Get-VdfField $Vdf.FullName 'publishedfileid'))"
    Write-UploadState $UploadState
    Write-DeploymentReport -Items $Vdfs -State $UploadState -RunResults $RunResults

    if ($DelaySeconds -gt 0 -and $Vdf.FullName -ne $Vdfs[-1].FullName) {
        Write-Host "Waiting $DelaySeconds seconds before the next item..."
        Start-Sleep -Seconds $DelaySeconds
    }
}

Write-Host ""
Write-Host "Deployment summary: $SuccessCount succeeded, $SkippedCount skipped, $PendingCount pending review, $FailedCount failed."
Write-Host "Detailed report: $DeployReportPath"

if ($FailedCount -gt 0) {
    exit 1
}
