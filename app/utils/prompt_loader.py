"""Prompt 模板加载器 — 统一加载和管理 Prompt 模板。"""
import yaml
from pathlib import Path
from app.utils.path_tool import resolve_path
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


class PromptLoader:
    """Prompt 模板加载器：从 YAML 配置读取模板路径，按需加载 Prompt 内容。"""

    def __init__(self, config_path: str = "app/config/prompt.yaml"):
        self._config_path = resolve_path(config_path)
        self._config = self._load_config(self._config_path)
        self._cache: dict[str, str] = {}

    def _load_config(self, config_path: Path) -> dict:
        try:
            with open(config_path, encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"加载 prompt.yaml 失败: {e}")
            return {"templates": {}}

    def load(self, name: str, **kwargs) -> str:
        """加载指定名称的 Prompt 模板。

        Args:
            name: 模板名称（如 'system', 'hyde', 'agent', 'summary', 'rewrite'）
            **kwargs: 运行时变量注入（如 query='...', chat_history='...', content='...'）

        Returns:
            填充后的 Prompt 字符串
        """
        template = self._read_template(name)
        if kwargs:
            try:
                return template.format(**kwargs)
            except KeyError as e:
                logger.debug(f"Prompt 模板变量缺失: {e}")
                return template
        return template

    def _read_template(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]

        templates = self._config.get("templates", {})
        template_path = templates.get(name)
        if not template_path:
            logger.warning(f"未找到 Prompt 模板: {name}")
            return ""

        try:
            full_path = resolve_path(template_path)
            with open(full_path, encoding="utf-8") as f:
                content = f.read()
            self._cache[name] = content
            return content
        except Exception as e:
            logger.error(f"加载 Prompt 模板 {name} 失败: {e}")
            return ""

    def reload(self) -> None:
        """清空缓存，强制重新加载所有模板。"""
        self._cache.clear()
        self._config = self._load_config(self._config_path)
