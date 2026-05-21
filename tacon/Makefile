# tacon developer tasks.

.PHONY: gui-dev

# Build the v0.3 web GUI (Vite SPA) into tacon/web/dist/ so `tacon serve`
# serves it at /. Run once after checkout, then whenever tacon/web/ changes.
# Needs Node >= 20 and pnpm >= 9 on PATH.
gui-dev:
	cd tacon/web && pnpm install && pnpm build
