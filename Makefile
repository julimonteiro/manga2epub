.PHONY: install run download download-all reset clean build clean-build help

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
