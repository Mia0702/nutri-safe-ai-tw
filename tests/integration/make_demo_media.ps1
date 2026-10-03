# TEST_ONLY: generate synthetic demo media with Windows built-ins (no extra packages).
# Inputs (env): DEMO_KIND = image | audio, DEMO_TEXT = text to draw / speak, DEMO_OUT = output path
$ErrorActionPreference = "Stop"
$kind = $env:DEMO_KIND
$text = $env:DEMO_TEXT
$out = $env:DEMO_OUT

if ($kind -eq "image") {
    Add-Type -AssemblyName System.Drawing
    $bmp = New-Object System.Drawing.Bitmap 900, 500
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::White)
    $font = New-Object System.Drawing.Font("Microsoft JhengHei", 34, [System.Drawing.FontStyle]::Bold)
    $rect = New-Object System.Drawing.RectangleF 40, 40, 820, 420
    $g.DrawString($text, $font, [System.Drawing.Brushes]::Black, $rect)
    $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
}
elseif ($kind -eq "audio") {
    Add-Type -AssemblyName System.Speech
    $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $voice = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq "zh-TW" } | Select-Object -First 1
    if ($null -eq $voice) { Write-Error "no zh-TW voice" }
    $s.SelectVoice($voice.VoiceInfo.Name)
    $s.SetOutputToWaveFile($out)
    $s.Speak($text)
    $s.Dispose()
}
else { Write-Error "unknown DEMO_KIND" }
