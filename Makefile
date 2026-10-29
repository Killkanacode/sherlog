.PHONY: up down nuke ps logs build smoke

up:            ## Build and start the whole stack
	docker compose up -d --build

down:          ## Stop the stack, keep data
	docker compose down

nuke:          ## Stop the stack and delete all volumes (re-seeds on next start)
	docker compose down -v

ps:
	docker compose ps

logs:          ## Follow logs of one service: make logs s=orders
	docker compose logs -f $(s)

build:
	docker compose build

smoke:         ## One checkout through the gateway
	@port=$$(grep -E '^GATEWAY_HOST_PORT=' .env 2>/dev/null | cut -d= -f2); \
	curl -sf -X POST "localhost:$${port:-8000}/checkout" -H 'content-type: application/json' \
	  -d '{"user_id":"smoke","items":[{"sku":"SKU-0000042","qty":1}]}' && echo
