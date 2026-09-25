.PHONY: install backend frontend check check-data check-api check-build

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 养护材料交付自检：数据准备 → 接口自检 → 前端构建，可重复执行
check:
	python3 scripts/check_pipeline.py

# 某个环节失败时，只重跑该环节
check-data:
	python3 scripts/check_pipeline.py --only data

check-api:
	python3 scripts/check_pipeline.py --only api

check-build:
	python3 scripts/check_pipeline.py --only build
