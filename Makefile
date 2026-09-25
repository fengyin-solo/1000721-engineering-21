.PHONY: install backend frontend check check-seed check-api check-frontend

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 养护材料流水线：示例数据准备 → 后端接口自检 → 前端构建检查
check:
	python3 scripts/run_pipeline.py

# 单个环节失败后只重跑该环节
check-seed:
	python3 scripts/run_pipeline.py --only seed

check-api:
	python3 scripts/run_pipeline.py --only api

check-frontend:
	python3 scripts/run_pipeline.py --only frontend
