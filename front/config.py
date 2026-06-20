"""前端配置模块。"""
import os

API_BASE_URL = os.getenv("RAG_API_BASE", "http://127.0.0.1:8000")
USER_ID = "default_user"
