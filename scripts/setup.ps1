if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not installed. Install it from https://astral.sh/uv before running this script."
    exit 1
}
uv sync
uv run python -m spacy download de_core_news_md
uv run python -m spacy download en_core_web_md
Write-Host "Setup complete. Run scripts\run.ps1 to start the app."
