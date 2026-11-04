.PHONY: webapp webapp-service webapp-service-remove clean-read schedule unschedule export-mihon install run download download-all reset clean build clean-build help

# ── Default target ──
help: ## Show available commands
	@echo ""
	@echo "  Manga Downloader - Available commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36mmake %-15s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── Setup ──
install: ## Install Python dependencies
	python3 -m pip install -r requirements.txt

# ── Interactive menu ──
run: ## Start the interactive terminal menu
	python3 manga_downloader.py

# ── Non-interactive CLI ──
download: ## Download new chapters (non-interactive)
	python3 manga_downloader.py --download

download-all: ## Re-download all chapters (non-interactive)
	python3 manga_downloader.py --download --all

reset: ## Reset download history
	python3 manga_downloader.py --reset

# ── Daily job (launchd, survives reboots) ──
schedule: ## Install daily download job (make schedule TIME=08:00)
	scripts/install_daily_job.sh $(TIME)

unschedule: ## Remove the daily download job
	scripts/install_daily_job.sh uninstall

export-mihon: ## Export existing EPUBs as CBZ into mihon/local
	python3 manga_downloader.py --export-mihon

# ── Web app (manage the list from the phone) ──
webapp: ## Run the web app in the foreground (http://127.0.0.1:8766)
	python3 webapp.py

webapp-service: ## Keep the web app running in the background (starts at login)
	scripts/install_webapp_service.sh

webapp-service-remove: ## Stop and remove the web app background service
	scripts/install_webapp_service.sh uninstall

clean-read: ## Delete EPUBs of chapters Mihon marks as read (DRY=1 to preview)
	python3 manga_downloader.py --clean-read $(if $(DRY),--dry-run)

# ── Build standalone executable ──
build: ## Build standalone executable (no Python needed to run)
	@echo "🔨 Building standalone executable..."
	pyinstaller manga_kindle.spec --noconfirm
	@echo ""
	@echo "✅ Build complete! Executable is at:"
	@echo "   dist/MangaKindle"
	@echo ""
	@echo "📋 To use: copy MangaKindle to any folder and run it."
	@echo "   It will create mangas.txt, downloads/ etc. in the current directory."

# ── Maintenance ──
clean: ## Remove all downloaded EPUBs and reset state
	rm -rf downloads/
	rm -f downloaded_chapters.json
	@echo "✅ Downloads and state cleared."

clean-build: ## Remove build artifacts (build/, dist/, __pycache__)
	rm -rf build/ dist/ __pycache__/ *.egg-info/
	@echo "✅ Build artifacts cleared."
