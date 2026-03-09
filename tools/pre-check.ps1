& "$PSScriptRoot/agent-fix-line-endings.ps1"
uv run isort .\src\
uv run black .\src\
uv run pylint .\src\
uv run ruff check .\src\
uv run mypy .\src\

$pytestArgs = @(".", "-q", "--durations=20")
uv run python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('xdist') else 1)"
if ($LASTEXITCODE -eq 0) {
    $pytestArgs = @(".", "-n", "auto", "--dist", "loadscope", "-q", "--durations=20")
}
uv run pytest @pytestArgs