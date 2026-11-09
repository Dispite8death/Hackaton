$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
Write-Host 'Запускаем Контур. Оставьте это окно открытым до окончания работы.'
$python = Join-Path $PWD '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $found = $false
    foreach ($command in @('py','python')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { continue }
        $options = @()
        if ($command -eq 'py') { $options = @('-3') }
        & $command @options -c 'import sys; sys.exit(sys.version_info < (3, 11))'
        if ($LASTEXITCODE -eq 0) {
            & $command @options -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
            $found = $true
            break
        }
    }
    if (-not $found) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw 'Установите Python 3.13 с python.org и снова запустите этот файл.'
        }
        & winget install --id Python.Python.3.13 --exact --scope user --accept-source-agreements --accept-package-agreements
        $installed = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
        if (-not (Test-Path -LiteralPath $installed)) { throw 'Установите Python 3.13 и повторите запуск.' }
        & $installed -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
    }
}
$requirements = [IO.File]::ReadAllText((Join-Path $PWD "requirements.txt"))
$stamp = '.venv\kontur-requirements.txt'
if (!(Test-Path $stamp) -or [IO.File]::ReadAllText((Join-Path $PWD $stamp)) -ne $requirements) {
    Write-Host 'Устанавливаем библиотеки. При первом запуске это может занять несколько минут.'
    & $python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить библиотеки. Проверьте интернет и повторите запуск.' }
    [IO.File]::WriteAllText((Join-Path $PWD $stamp), $requirements)
}
& $python -m Engine.site_server --open @args
exit $LASTEXITCODE
