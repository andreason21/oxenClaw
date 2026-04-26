<#
.SYNOPSIS
  Generate a self-signed Authenticode code-signing certificate for sampyClaw
  Windows MSI / NSIS bundles, export it to .pfx, and print the values you need
  to paste into GitHub repository secrets.

.DESCRIPTION
  Produces three artifacts in the chosen output directory:
    sampyclaw_codesign.cer        public certificate (distribute to users
                                  who want to import it as Trusted Publisher)
    sampyclaw_codesign.pfx        private+public bundle (KEEP SECRET)
    sampyclaw_codesign.pfx.b64    base64 of the .pfx (paste into the
                                  WINDOWS_CERT_PFX repo secret)

  Then prints the thumbprint and the base64 blob.

  IMPORTANT: a self-signed certificate does NOT silence Windows SmartScreen.
  SmartScreen blocks anything that doesn't chain to a CA in the Microsoft
  Trusted Root Program. End users will still see "Microsoft Defender
  SmartScreen prevented an unrecognised app from starting" unless they
  manually import sampyclaw_codesign.cer into their Trusted Publishers /
  Trusted Root store first.

  What self-signing DOES buy you:
    - Tamper detection (signature is tied to your private key).
    - "Unknown publisher" line in UAC dialogs is replaced with your
      Subject CN.
    - A consistent identity across builds, so machines that have imported
      your .cer once trust every future build automatically.

.PARAMETER Subject
  Subject CN for the cert. Default: "CN=sampyClaw, O=sampyClaw, C=KR".

.PARAMETER OutDir
  Where to drop the .cer / .pfx / .pfx.b64. Default: $env:USERPROFILE\sampyclaw-codesign.

.PARAMETER ValidYears
  Cert validity. Default 10 years — long enough that we don't have to
  rotate during the project's foreseeable lifetime.

.PARAMETER PfxPassword
  Password for the .pfx export. If omitted, prompts interactively. The
  same value goes into the WINDOWS_CERT_PASSWORD repo secret.

.EXAMPLE
  pwsh ./scripts/win/gen_selfsigned_codesign.ps1
#>
[CmdletBinding()]
param(
    [string] $Subject     = "CN=sampyClaw, O=sampyClaw, C=KR",
    [string] $OutDir      = (Join-Path $env:USERPROFILE "sampyclaw-codesign"),
    [int]    $ValidYears  = 10,
    [SecureString] $PfxPassword
)

$ErrorActionPreference = "Stop"

if (-not $PfxPassword) {
    $PfxPassword = Read-Host -AsSecureString -Prompt "PFX password (also used as WINDOWS_CERT_PASSWORD secret)"
}

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

$notAfter = (Get-Date).AddYears($ValidYears)

Write-Host "Creating self-signed Authenticode cert..." -ForegroundColor Cyan
$cert = New-SelfSignedCertificate `
    -Subject           $Subject `
    -Type              CodeSigningCert `
    -KeyUsage          DigitalSignature `
    -KeyAlgorithm      RSA `
    -KeyLength         3072 `
    -HashAlgorithm     SHA256 `
    -CertStoreLocation Cert:\CurrentUser\My `
    -NotAfter          $notAfter `
    -TextExtension     @("2.5.29.37={text}1.3.6.1.5.5.7.3.3")

$cerPath  = Join-Path $OutDir "sampyclaw_codesign.cer"
$pfxPath  = Join-Path $OutDir "sampyclaw_codesign.pfx"
$b64Path  = Join-Path $OutDir "sampyclaw_codesign.pfx.b64"

Write-Host "Exporting public .cer..." -ForegroundColor Cyan
Export-Certificate -Cert $cert -FilePath $cerPath -Type CERT | Out-Null

Write-Host "Exporting .pfx (private+public)..." -ForegroundColor Cyan
Export-PfxCertificate `
    -Cert     $cert `
    -FilePath $pfxPath `
    -Password $PfxPassword | Out-Null

Write-Host "Encoding .pfx as base64..." -ForegroundColor Cyan
$pfxBytes = [IO.File]::ReadAllBytes($pfxPath)
$b64      = [Convert]::ToBase64String($pfxBytes)
Set-Content -Path $b64Path -Value $b64 -NoNewline -Encoding ascii

Write-Host ""
Write-Host "===== sampyClaw self-signed code-signing cert =====" -ForegroundColor Green
Write-Host "  Subject     : $($cert.Subject)"
Write-Host "  Thumbprint  : $($cert.Thumbprint)"
Write-Host "  Not after   : $($cert.NotAfter)"
Write-Host "  Output dir  : $OutDir"
Write-Host "    .cer      : $cerPath"
Write-Host "    .pfx      : $pfxPath              (KEEP SECRET)"
Write-Host "    .pfx.b64  : $b64Path  (paste into WINDOWS_CERT_PFX)"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. GitHub -> Settings -> Secrets and variables -> Actions -> New repository secret"
Write-Host "       WINDOWS_CERT_PFX      = (contents of sampyclaw_codesign.pfx.b64)"
Write-Host "       WINDOWS_CERT_PASSWORD = (the password you just typed)"
Write-Host "  2. Cut a new release tag — the windows-build job's"
Write-Host "     'Optional code-signing' step will pick the secrets up"
Write-Host "     and signtool the .msi + NSIS .exe automatically."
Write-Host "  3. Distribute sampyclaw_codesign.cer to users who want to"
Write-Host "     pre-trust the cert (Import-Certificate to"
Write-Host "     Cert:\LocalMachine\TrustedPublisher and"
Write-Host "     Cert:\LocalMachine\Root). Otherwise SmartScreen will"
Write-Host "     still warn — that's the cost of self-signing."
