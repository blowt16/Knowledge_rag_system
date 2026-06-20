"""视觉模型调用服务 — 多模态 LLM 图片描述。"""
import os
import base64
from app.utils.log_tool import get_logger
from app.utils.prompt_loader import PromptLoader

logger = get_logger(__name__)


def _get_vision_prompt() -> str:
    loader = PromptLoader()
    prompt = loader.load("vision")
    if prompt:
        return prompt
    return "请详细描述这张图片的内容。如果是图表，请提取其中的数据；如果是文档截图，请识别其中的文字；如果是图片，请描述其主要内容。"


class VisionService:
    """多模态视觉模型服务 — 支持阿里云百炼 / 本地 Ollama，自动降级。"""

    def __init__(self):
        self._model = None
        self._model_type = os.getenv("VISION_MODEL_TYPE", "ALIYUN").upper()

    def _get_model(self):
        if self._model is None:
            try:
                from app.utils.factory import create_vision_model
                self._model = create_vision_model()
                logger.info(f"【视觉服务】模型 {self._model_type} 已就绪")
            except Exception as e:
                logger.error(f"【视觉服务】模型初始化失败: {e}")
                self._model = None
        return self._model

    async def describe_image(self, image_path: str) -> str:
        model = self._get_model()
        if model is None:
            return ""

        try:
            from langchain_core.messages import HumanMessage

            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")

            ext = os.path.splitext(image_path)[1].lower().lstrip(".")
            mime_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}
            mime_type = f"image/{mime_map.get(ext, 'png')}"

            message = HumanMessage(content=[
                {"type": "text", "text": _get_vision_prompt()},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}},
            ])

            response = await model.ainvoke([message])
            result = response.content if hasattr(response, "content") else str(response)
            logger.info(f"【视觉服务】图片描述成功: {image_path}")
            return result.strip()
        except Exception as e:
            logger.warning(f"【视觉服务】描述图片失败 ({image_path}): {e}")
            return ""

    async def describe_image_batch(self, image_paths: list[str], batch_size: int = 5) -> dict[str, str]:
        import asyncio
        results = {}
        for i in range(0, len(image_paths), batch_size):
            batch = image_paths[i:i + batch_size]
            tasks = [self.describe_image(path) for path in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for path, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.warning(f"【视觉服务】批量描述异常: {path} -> {result}")
                    results[path] = ""
                else:
                    results[path] = result
        return results
