FAST_AGENT_SRC := backend/fast-agent

.PHONY: deploy build clean update-submodule

# Update fast-agent submodule + build Docker
deploy:
	@echo "→ Updating fast-agent submodule..."
	git submodule update --remote backend/fast-agent
	@echo "→ Building Docker..."
	docker compose up -d --build --force-recreate jarvis-backend
	@echo "✅ Done"

# Build both services
build:
	@echo "→ Updating fast-agent submodule..."
	git submodule update --remote backend/fast-agent
	@echo "→ Building Docker..."
	docker compose up -d --build
	@echo "✅ Done"

# Update submodule only
update-submodule:
	git submodule update --remote backend/fast-agent
