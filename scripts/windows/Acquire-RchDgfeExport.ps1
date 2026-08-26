#requires -Version 5.1
<#
.SYNOPSIS
    Acquires RCH XTools DGFE exports into a private, content-addressed outbox.

.DESCRIPTION
    This Windows-only companion launches RCH XTools optionally, watches its
    export directory, snapshots new fiscal-memory XML files after a bounded
    quiet period, validates their root/version/matricola with a hardened XML
    reader, and writes byte-identical copies, SHA-256 sidecars and an audit
    manifest atomically.

    It deliberately does not automate the XTools UI, open a network socket,
    know a printer address, or send a fiscal command. An authorized operator
    must select "Funzioni Cassa -> Letture DGFE -> Export V11 XML" in XTools.
#>

[CmdletBinding(DefaultParameterSetName = "Watch")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Watch")]
    [ValidateNotNullOrEmpty()]
    [string]$ExportDirectory,

    [Parameter(Mandatory = $true, ParameterSetName = "File")]
    [ValidateNotNullOrEmpty()]
    [string[]]$SourceFile,

    [Parameter(ParameterSetName = "Watch")]
    [switch]$LaunchXTools,

    [Parameter(ParameterSetName = "Watch")]
    [string]$XToolsPath,

    [string]$OutboxDirectory,

    [ValidatePattern("^[A-Za-z0-9]{10,32}$")]
    [string]$ExpectedRchSerial,

    [ValidateRange(1, 86400)]
    [int]$TimeoutSeconds = 900,

    [ValidateRange(1, 120)]
    [int]$StableSeconds = 10,

    [ValidateRange(1, 1000)]
    [int]$ExpectedFileCount = 1,

    [ValidateRange(1024, 67108864)]
    [long]$MaximumBytes = 67108864
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$detailNamespace = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/memoriadettaglio/export/v2.0"
$summaryNamespace = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/memoriariepilogo/export/v2.0"
$xmlDsigNamespace = "http://www.w3.org/2000/09/xmldsig#"
$allowedVersion = "3.0"
$maximumElements = 250000
$maximumDepth = 64
$maximumAttributes = 32

function Write-OperatorMessage {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine("[RetailPrintGuard DGFE] {0}", $Message)
}

function Get-NormalizedFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "A required path is empty."
    }
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-NotReparsePoint {
    param([Parameter(Mandatory = $true)][System.IO.FileSystemInfo]$Item)
    if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Reparse points are not accepted as fiscal evidence paths: $($Item.FullName)"
    }
}

function Set-PrivateDirectoryAcl {
    param([Parameter(Mandatory = $true)][System.IO.DirectoryInfo]$Directory)

    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $propagation = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
    $identities = @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User,
        (New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")),
        (New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544"))
    )
    foreach ($identity in $identities) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $identity,
            $fullControl,
            $inheritance,
            $propagation,
            $allow
        )
        [void]$security.AddAccessRule($rule)
    }
    $Directory.SetAccessControl($security)
}

function Assert-PrivateDirectoryAcl {
    param([Parameter(Mandatory = $true)][System.IO.DirectoryInfo]$Directory)

    $broadSids = @("S-1-1-0", "S-1-5-11", "S-1-5-32-545")
    $acl = $Directory.GetAccessControl(
        [System.Security.AccessControl.AccessControlSections]::Access
    )
    $rules = $acl.GetAccessRules(
        $true,
        $true,
        [System.Security.Principal.SecurityIdentifier]
    )
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        $isAllow = $rule.AccessControlType -eq `
            [System.Security.AccessControl.AccessControlType]::Allow
        $canWrite = ($rule.FileSystemRights -band `
            [System.Security.AccessControl.FileSystemRights]::Write) -ne 0
        if ($isAllow -and $canWrite -and $broadSids -contains $sid) {
            throw "Outbox grants write access to a broad principal ($sid): $($Directory.FullName)"
        }
    }
}

function Get-OrCreatePrivateOutbox {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = Get-NormalizedFullPath -Path $Path
    $created = $false
    if (-not [System.IO.Directory]::Exists($fullPath)) {
        $directory = [System.IO.Directory]::CreateDirectory($fullPath)
        $created = $true
    } else {
        $directory = Get-Item -LiteralPath $fullPath -Force
    }
    if (-not ($directory -is [System.IO.DirectoryInfo])) {
        throw "Outbox is not a directory: $fullPath"
    }
    Assert-NotReparsePoint -Item $directory
    if ($created) {
        Set-PrivateDirectoryAcl -Directory $directory
    }
    Assert-PrivateDirectoryAcl -Directory $directory
    return $directory
}

function Get-XToolsAuthenticodeSignature {
    param([Parameter(Mandatory = $true)][System.IO.FileInfo]$Executable)

    # Do not trust PSModulePath ordering inherited from another PowerShell host.
    # Load the security module shipped with this exact Windows PowerShell runtime.
    $securityModule = Join-Path $PSHOME `
        "Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
    if (-not [System.IO.File]::Exists($securityModule)) {
        throw "The built-in PowerShell security module is unavailable."
    }
    Import-Module -Name $securityModule -Force -ErrorAction Stop
    return Microsoft.PowerShell.Security\Get-AuthenticodeSignature `
        -LiteralPath $Executable.FullName
}

function Get-FileState {
    param([Parameter(Mandatory = $true)][System.IO.FileInfo]$File)
    return "{0}|{1}|{2}" -f $File.FullName, $File.Length, $File.LastWriteTimeUtc.Ticks
}

function Get-DirectoryXmlState {
    param([Parameter(Mandatory = $true)][System.IO.DirectoryInfo]$Directory)
    $state = @{}
    foreach ($file in Get-ChildItem -LiteralPath $Directory.FullName -Filter "*.xml" -File) {
        Assert-NotReparsePoint -Item $file
        $state[$file.FullName] = Get-FileState -File $file
    }
    return $state
}

function Wait-NewStableXmlFiles {
    param(
        [Parameter(Mandatory = $true)][System.IO.DirectoryInfo]$Directory,
        [Parameter(Mandatory = $true)][hashtable]$Baseline,
        [Parameter(Mandatory = $true)][datetime]$DeadlineUtc
    )

    $lastSignature = $null
    $quietSinceUtc = $null
    while ([datetime]::UtcNow -lt $DeadlineUtc) {
        $candidates = @()
        foreach ($file in Get-ChildItem -LiteralPath $Directory.FullName -Filter "*.xml" -File) {
            Assert-NotReparsePoint -Item $file
            $state = Get-FileState -File $file
            if (-not $Baseline.ContainsKey($file.FullName) -or $Baseline[$file.FullName] -ne $state) {
                $candidates += $file
            }
        }
        $states = @($candidates | Sort-Object FullName | ForEach-Object { Get-FileState -File $_ })
        $signature = [string]::Join("`n", $states)
        if ($candidates.Count -ge $ExpectedFileCount) {
            if ($signature -ne $lastSignature) {
                $lastSignature = $signature
                $quietSinceUtc = [datetime]::UtcNow
            } elseif ($null -ne $quietSinceUtc -and `
                ([datetime]::UtcNow - $quietSinceUtc).TotalSeconds -ge $StableSeconds) {
                return @($candidates | Sort-Object FullName)
            }
        } else {
            $lastSignature = $signature
            $quietSinceUtc = $null
        }
        Start-Sleep -Milliseconds 500
    }
    throw "No complete XTools XML export became stable within $TimeoutSeconds seconds."
}

function Wait-SpecifiedStableFiles {
    param(
        [Parameter(Mandatory = $true)][string[]]$Paths,
        [Parameter(Mandatory = $true)][datetime]$DeadlineUtc
    )

    $lastSignature = $null
    $quietSinceUtc = $null
    while ([datetime]::UtcNow -lt $DeadlineUtc) {
        $files = @()
        foreach ($path in $Paths) {
            $fullPath = Get-NormalizedFullPath -Path $path
            if (-not [System.IO.File]::Exists($fullPath)) {
                throw "Source XML does not exist: $fullPath"
            }
            $file = Get-Item -LiteralPath $fullPath -Force
            if (-not ($file -is [System.IO.FileInfo]) -or $file.Extension -ine ".xml") {
                throw "Source must be a regular .xml file: $fullPath"
            }
            Assert-NotReparsePoint -Item $file
            $files += $file
        }
        $states = @($files | Sort-Object FullName | ForEach-Object { Get-FileState -File $_ })
        $signature = [string]::Join("`n", $states)
        if ($signature -ne $lastSignature) {
            $lastSignature = $signature
            $quietSinceUtc = [datetime]::UtcNow
        } elseif (([datetime]::UtcNow - $quietSinceUtc).TotalSeconds -ge $StableSeconds) {
            return @($files | Sort-Object FullName)
        }
        Start-Sleep -Milliseconds 500
    }
    throw "The specified XML files did not become stable within $TimeoutSeconds seconds."
}

function Read-ExclusiveSnapshot {
    param([Parameter(Mandatory = $true)][System.IO.FileInfo]$File)

    Assert-NotReparsePoint -Item $File
    $stream = [System.IO.File]::Open(
        $File.FullName,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    try {
        if ($stream.Length -lt 1 -or $stream.Length -gt $MaximumBytes) {
            throw "XML size is outside the supported range: $($File.FullName)"
        }
        $memory = New-Object System.IO.MemoryStream
        try {
            $buffer = New-Object byte[] 1048576
            while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $memory.Write($buffer, 0, $read)
                if ($memory.Length -gt $MaximumBytes) {
                    throw "XML exceeds the $MaximumBytes-byte limit."
                }
            }
            return $memory.ToArray()
        } finally {
            $memory.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Read-FiscalMemoryXmlMetadata {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $settings = New-Object System.Xml.XmlReaderSettings
    $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $settings.MaxCharactersFromEntities = 0
    $settings.MaxCharactersInDocument = $MaximumBytes
    $settings.IgnoreComments = $false
    $settings.IgnoreProcessingInstructions = $false

    $memory = New-Object System.IO.MemoryStream(, $Bytes)
    $reader = $null
    try {
        $reader = [System.Xml.XmlReader]::Create($memory, $settings)
        $rootName = $null
        $rootNamespace = $null
        $version = $null
        $signaturePresent = $false
        $serials = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
        $elementCount = 0
        while ($reader.Read()) {
            if ($reader.NodeType -ne [System.Xml.XmlNodeType]::Element) {
                continue
            }
            $elementCount += 1
            if ($elementCount -gt $maximumElements) {
                throw "XML exceeds the $maximumElements-element limit."
            }
            if ($reader.Depth -gt $maximumDepth) {
                throw "XML exceeds the maximum depth of $maximumDepth."
            }
            if ($reader.AttributeCount -gt $maximumAttributes) {
                throw "An XML element exceeds the $maximumAttributes-attribute limit."
            }
            if ($null -eq $rootName) {
                if ($reader.Depth -ne 0) {
                    throw "XML root was not found at depth zero."
                }
                $rootName = $reader.LocalName
                $rootNamespace = $reader.NamespaceURI
                $version = $reader.GetAttribute("versione")
            }
            if ($reader.NamespaceURI -eq $xmlDsigNamespace -and $reader.LocalName -eq "Signature") {
                $signaturePresent = $true
            }
            if ($reader.LocalName -eq "MatricolaRT") {
                $serial = $reader.ReadElementContentAsString().Trim().ToUpperInvariant()
                if ($serial -notmatch "^[A-Z0-9]{10,32}$") {
                    throw "MatricolaRT is missing or malformed."
                }
                [void]$serials.Add($serial)
            }
        }
        if ($rootName -eq "MemoriaDettaglio" -and $rootNamespace -eq $detailNamespace) {
            $kind = "detail"
        } elseif ($rootName -eq "MemoriaRiepilogo" -and $rootNamespace -eq $summaryNamespace) {
            $kind = "summary"
        } else {
            throw "Unsupported fiscal-memory XML root: {$rootNamespace}$rootName"
        }
        if ($version -ne $allowedVersion) {
            throw "Unsupported fiscal-memory payload version: $version"
        }
        if ($serials.Count -ne 1) {
            throw "XML must identify exactly one MatricolaRT."
        }
        $serialValue = @($serials)[0]
        if (-not [string]::IsNullOrWhiteSpace($ExpectedRchSerial) -and `
            $serialValue -ne $ExpectedRchSerial.Trim().ToUpperInvariant()) {
            throw "MatricolaRT $serialValue does not match the configured RCH."
        }
        return [pscustomobject]@{
            kind = $kind
            root_name = $rootName
            namespace = $rootNamespace
            payload_version = $version
            rch_serial = $serialValue
            xml_signature_present = $signaturePresent
            xml_signature_verification = "NOT_PERFORMED_NO_TRUST_CHAIN"
        }
    } finally {
        if ($null -ne $reader) {
            $reader.Dispose()
        }
        $memory.Dispose()
    }
}

function Write-BytesAtomically {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedHash
    )

    if ([System.IO.File]::Exists($Destination)) {
        $existing = [System.IO.File]::ReadAllBytes($Destination)
        if ((Get-Sha256Hex -Bytes $existing) -ne $ExpectedHash) {
            throw "Existing content-addressed artifact has a different hash: $Destination"
        }
        return
    }
    $temporary = "$Destination.part-$([guid]::NewGuid().ToString('N'))"
    try {
        [System.IO.File]::WriteAllBytes($temporary, $Bytes)
        $written = [System.IO.File]::ReadAllBytes($temporary)
        if ((Get-Sha256Hex -Bytes $written) -ne $ExpectedHash) {
            throw "Staged artifact failed SHA-256 verification."
        }
        [System.IO.File]::Move($temporary, $Destination)
    } finally {
        if ([System.IO.File]::Exists($temporary)) {
            [System.IO.File]::Delete($temporary)
        }
    }
}

function Write-Utf8TextAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $temporary = "$Destination.part-$([guid]::NewGuid().ToString('N'))"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    try {
        [System.IO.File]::WriteAllText($temporary, $Text, $encoding)
        if ([System.IO.File]::Exists($Destination)) {
            throw "Refusing to overwrite an existing audit artifact: $Destination"
        }
        [System.IO.File]::Move($temporary, $Destination)
    } finally {
        if ([System.IO.File]::Exists($temporary)) {
            [System.IO.File]::Delete($temporary)
        }
    }
}

try {
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw "Acquire-RchDgfeExport.ps1 is supported only on Windows."
    }

    if ([string]::IsNullOrWhiteSpace($OutboxDirectory)) {
        $base = if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            $env:LOCALAPPDATA
        } else {
            $env:USERPROFILE
        }
        $OutboxDirectory = Join-Path $base "RetailPrintGuard\RchDgfe\outbox"
    }
    $outbox = Get-OrCreatePrivateOutbox -Path $OutboxDirectory
    $deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)

    if ($PSCmdlet.ParameterSetName -eq "Watch") {
        $exportPath = Get-NormalizedFullPath -Path $ExportDirectory
        if (-not [System.IO.Directory]::Exists($exportPath)) {
            throw "XTools export directory does not exist: $exportPath"
        }
        $export = Get-Item -LiteralPath $exportPath -Force
        Assert-NotReparsePoint -Item $export
        $baseline = Get-DirectoryXmlState -Directory $export

        if ($LaunchXTools) {
            if ([string]::IsNullOrWhiteSpace($XToolsPath)) {
                throw "-LaunchXTools requires an explicit -XToolsPath."
            }
            $resolvedXTools = Get-NormalizedFullPath -Path $XToolsPath
            if (-not [System.IO.File]::Exists($resolvedXTools) -or `
                [System.IO.Path]::GetExtension($resolvedXTools) -ine ".exe") {
                throw "XTools executable does not exist: $resolvedXTools"
            }
            $xtools = Get-Item -LiteralPath $resolvedXTools -Force
            Assert-NotReparsePoint -Item $xtools
            $authenticode = Get-XToolsAuthenticodeSignature -Executable $xtools
            if ($authenticode.Status -ne "Valid") {
                throw "XTools Authenticode signature is not valid: $($authenticode.Status)"
            }
            [void](Start-Process -FilePath $xtools.FullName -PassThru)
        }

        Write-OperatorMessage "In XTools select only: Funzioni Cassa -> Letture DGFE -> Export V11 XML."
        Write-OperatorMessage "Waiting for $ExpectedFileCount new or changed XML file(s) in $exportPath"
        $files = Wait-NewStableXmlFiles -Directory $export -Baseline $baseline -DeadlineUtc $deadline
    } else {
        $files = Wait-SpecifiedStableFiles -Paths $SourceFile -DeadlineUtc $deadline
    }

    $acquiredAt = [datetime]::UtcNow
    $artifacts = @()
    foreach ($file in $files) {
        $bytes = Read-ExclusiveSnapshot -File $file
        $hash = Get-Sha256Hex -Bytes $bytes
        $metadata = Read-FiscalMemoryXmlMetadata -Bytes $bytes
        $artifactName = "rch-dgfe-{0}-{1}.xml" -f $metadata.kind, $hash
        $artifactPath = Join-Path $outbox.FullName $artifactName
        Write-BytesAtomically -Bytes $bytes -Destination $artifactPath -ExpectedHash $hash

        $sidecarPath = "$artifactPath.sha256"
        $sidecarText = "$hash  $artifactName`n"
        if ([System.IO.File]::Exists($sidecarPath)) {
            $currentSidecar = [System.IO.File]::ReadAllText($sidecarPath)
            if ($currentSidecar -ne $sidecarText) {
                throw "Existing SHA-256 sidecar is inconsistent: $sidecarPath"
            }
        } else {
            Write-Utf8TextAtomically -Text $sidecarText -Destination $sidecarPath
        }

        $artifacts += [pscustomobject]@{
            source_file_name = $file.Name
            outbox_file = $artifactName
            sha256 = $hash
            size_bytes = $bytes.LongLength
            source_last_write_utc = $file.LastWriteTimeUtc.ToString("o")
            root_name = $metadata.root_name
            namespace = $metadata.namespace
            payload_version = $metadata.payload_version
            export_kind = $metadata.kind
            rch_serial = $metadata.rch_serial
            xml_signature_present = $metadata.xml_signature_present
            xml_signature_verification = $metadata.xml_signature_verification
        }
    }

    $runId = "{0}-{1}" -f $acquiredAt.ToString("yyyyMMddTHHmmssfffZ"), `
        ([guid]::NewGuid().ToString("N").Substring(0, 12))
    $manifestName = "rch-dgfe-acquisition-$runId.json"
    $manifestPath = Join-Path $outbox.FullName $manifestName
    $manifest = [ordered]@{
        schema_version = 1
        tool = "RetailPrintGuard Windows DGFE acquisition companion"
        acquisition_mode = if ($PSCmdlet.ParameterSetName -eq "Watch") { "XTOOLS_WATCH" } else { "EXISTING_FILE" }
        acquired_at_utc = $acquiredAt.ToString("o")
        x_tools_launched = [bool]$LaunchXTools
        outbox = $outbox.FullName
        artifact_count = $artifacts.Count
        artifacts = $artifacts
        guarantees = @(
            "BYTE_IDENTICAL_COPY",
            "SHA256_COMPUTED",
            "XML_ROOT_AND_VERSION_VALIDATED",
            "NO_DEVICE_SOCKET_OPENED",
            "NO_FISCAL_COMMAND_SENT"
        )
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 8
    Write-Utf8TextAtomically -Text ($manifestJson + "`n") -Destination $manifestPath

    $result = [ordered]@{
        status = "ok"
        artifact_count = $artifacts.Count
        outbox = $outbox.FullName
        manifest = $manifestName
        artifacts = @($artifacts | ForEach-Object { $_.outbox_file })
    }
    $result | ConvertTo-Json -Depth 4
    exit 0
} catch {
    [Console]::Error.WriteLine("[RetailPrintGuard DGFE] ERROR: {0}", $_.Exception.Message)
    exit 1
}
