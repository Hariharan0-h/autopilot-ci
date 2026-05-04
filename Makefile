.PHONY: install demo run test lint clean

install:
	pip install -r requirements.txt

demo:
	python demo/seed_bugs.py
	@echo "Starting webhook server on :8000..."
	uvicorn server.webhook:app --port 8000 --reload &
	@sleep 2
	@echo "Starting dashboard on :7860..."
	python dashboard/app.py

run:
	@test -n "$(REPO)" || (echo "Usage: make run REPO=/path BASE=abc HEAD=def" && exit 1)
	python -c "\
import asyncio; \
from schemas import WebhookPayload; \
from pipeline.runner import run_pipeline; \
import uuid; \
q = asyncio.Queue(); \
p = WebhookPayload(repo_path='$(REPO)', base='$(BASE)', head='$(HEAD)'); \
asyncio.run(run_pipeline(p, str(uuid.uuid4()), q)); \
"

test:
	pytest tests/ -v

lint:
	python -m bandit -r . --exclude demo/,tests/

clean:
	rm -rf demo/sample_repo/.git
	rm -rf /tmp/generated_tests/
	find . -name "__pycache__" -exec rm -rf {} +
