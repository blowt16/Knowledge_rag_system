"""Markdown 结构化解析器 — mistune v3 AST + 正则降级。"""
import re
from app.utils.log_tool import get_logger

logger = get_logger(__name__)


def _strip_markdown_markup(text: str) -> str:
    """自定义正则剥离所有 MD 标记，降级路径用。"""
    lines = text.split('\n')
    stripped = []
    for line in lines:
        s = line

        # 1. 图片 ![alt](url) → [图]
        s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '[图]', s)
        # 2. 链接 [text](url) → text
        s = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', s)
        # 3. 加粗 **text** 或 __text__
        s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
        s = re.sub(r'__(.+?)__', r'\1', s)
        # 4. 斜体 *text* 或 _text_（排除 ** 和 __）
        s = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', s)
        s = re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'\1', s)
        # 5. 删除线 ~~text~~
        s = re.sub(r'~~(.+?)~~', r'\1', s)
        # 6. 行内代码 `code`
        s = re.sub(r'`([^`]+)`', r'\1', s)
        # 7. 标题标记 ^#{1,6}\s+
        s = re.sub(r'^#{1,6}\s+', '', s)
        # 8. 引用标记 ^>\s?
        s = re.sub(r'^>\s?', '', s)
        # 9. 无序列表标记
        s = re.sub(r'^[\-\*\+]\s+', '', s)
        # 10. 有序列表标记 ^\d+\.\s+
        s = re.sub(r'^\d+\.\s+', '', s)
        # 11. 水平线
        s = re.sub(r'^[\-\*\_]{3,}\s*$', '', s)
        # 12. HTML 标签
        s = re.sub(r'<[^>]*>', '', s)

        stripped.append(s)

    text = '\n'.join(stripped)
    # 压缩多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


class MdStructuredParser:
    """mistune v3 AST 结构化解析器。

    提取完整章节层级 (TOC)、表格、列表、代码块等结构，
    剥离内联格式化标记（加粗、斜体、链接等），输出适合 RAG 的纯文本。
    """

    def __init__(self):
        self.toc: list[dict] = []
        self._chapter_path: list[str] = []
        self._lines: list[str] = []

    def parse(self, md_text: str) -> tuple[str, list[dict]]:
        self.toc.clear()
        self._chapter_path.clear()
        self._lines.clear()

        import mistune
        md = mistune.create_markdown(
            plugins=['table', 'task_lists', 'strikethrough', 'footnotes'],
            renderer='ast',
        )
        ast = md(md_text)
        self._walk(ast)
        return '\n\n'.join(self._lines), list(self.toc)

    def _walk(self, nodes: list):
        for node in nodes:
            self._dispatch(node)

    def _dispatch(self, node: dict):
        node_type = node.get('type', '')

        if node_type == 'heading':
            self._handle_heading(node)
        elif node_type == 'paragraph':
            self._handle_paragraph(node)
        elif node_type == 'block_code':
            self._handle_block_code(node)
        elif node_type == 'table':
            self._handle_table(node)
        elif node_type == 'list':
            self._handle_list(node)
        elif node_type == 'block_quote':
            self._handle_block_quote(node)
        elif node_type == 'thematic_break':
            self._lines.append('---')
        elif node_type in ('block_html', 'html'):
            pass
        elif node_type == 'blank_line':
            pass

    def _handle_heading(self, node: dict):
        level = node['attrs']['level']
        text = self._collect_text(node)

        while len(self._chapter_path) >= level:
            self._chapter_path.pop()
        self._chapter_path.append(text)

        path = ' > '.join(self._chapter_path)
        self.toc.append({'level': level, 'text': text, 'path': path})
        self._lines.append(f"{'#' * level} {text}")

    def _handle_paragraph(self, node: dict):
        text = self._render_inline(node)
        if text.strip():
            self._lines.append(text)

    def _handle_block_code(self, node: dict):
        info = node.get('attrs', {}).get('info', '')
        code = node.get('raw', '').rstrip('\n')
        if info:
            self._lines.append(f"[代码块 {info}]\n{code}\n[/代码块]")
        else:
            self._lines.append(f"[代码块]\n{code}\n[/代码块]")

    def _handle_table(self, node: dict):
        children = node.get('children', [])
        if len(children) < 2:
            return
        head_section = children[0]  # table_head: children = [cell, cell, ...]
        body_section = children[1]  # table_body: children = [row, row, ...]

        def render_cell(cell: dict) -> str:
            return self._collect_text(cell).replace('|', '\\|').replace('\n', ' ')

        def render_row(row: dict) -> str:
            cells = [render_cell(c) for c in row.get('children', [])]
            return '| ' + ' | '.join(cells) + ' |'

        rows = []
        # head: cells 是 table_head 的直接子节点
        if head_section.get('children'):
            head_cells = head_section['children']
            rows.append('| ' + ' | '.join(render_cell(c) for c in head_cells) + ' |')
            rows.append('|' + ' --- |' * len(head_cells))

        # body: rows 是 table_body 的子节点，每个 row 包裹 cells
        if body_section.get('children'):
            for row_token in body_section['children']:
                rows.append(render_row(row_token))

        self._lines.append('\n'.join(rows))

    def _handle_list(self, node: dict):
        ordered = node.get('attrs', {}).get('ordered', False)
        depth = node.get('attrs', {}).get('depth', 0)
        items = node.get('children', [])
        indent = '    ' * depth
        count = 0
        for item in items:
            item_type = item.get('type', '')
            if item_type not in ('list_item', 'task_list_item'):
                continue
            count += 1
            checked = item.get('attrs', {}).get('checked')
            bullet = '•'
            if item_type == 'task_list_item':
                bullet = '☑' if checked else '☐'
            elif ordered:
                bullet = f'{count}.'

            # 只收集 block_text 的文本，跳过嵌套子列表
            text_parts = []
            nested_lists = []
            for child in item.get('children', []):
                if isinstance(child, dict) and child.get('type') == 'list':
                    nested_lists.append(child)
                else:
                    text_parts.append(self._collect_text(child))
            text = ''.join(text_parts).strip()
            self._lines.append(f"{indent}{bullet} {text}")
            # 递归处理嵌套子列表
            for nested in nested_lists:
                nested['attrs'] = dict(nested.get('attrs', {}))
                nested['attrs']['depth'] = depth + 1
                self._handle_list(nested)

    def _handle_block_quote(self, node: dict):
        saved_lines = self._lines
        self._lines = []
        self._walk(node.get('children', []))
        quoted = self._lines
        self._lines = saved_lines
        for line in '\n'.join(quoted).split('\n'):
            if line.strip():
                self._lines.append(f"> {line}")

    def _render_inline(self, node: dict) -> str:
        """递归渲染内联元素，剥离格式化标记。"""
        node_type = node.get('type', '')

        if node_type == 'text':
            return node.get('raw', '')
        if node_type == 'softbreak':
            return ' '
        if node_type == 'linebreak':
            return '\n'
        if node_type == 'codespan':
            return node.get('raw', '')
        if node_type in ('html', 'block_html'):
            return ''
        if node_type == 'footnote_ref':
            return ''
        if node_type == 'image':
            alt = self._render_children(node.get('children', []))
            return f'[图: {alt}]' if alt else '[图]'
        if node_type == 'link':
            return self._render_children(node.get('children', []))

        # 剥离格式化标记
        children = node.get('children', [])
        if children:
            return self._render_children(children)
        return node.get('raw', '')

    def _render_children(self, children: list) -> str:
        parts = []
        for child in children:
            if isinstance(child, dict):
                parts.append(self._render_inline(child))
            elif isinstance(child, str):
                parts.append(child)
        return ''.join(parts)

    def _collect_text(self, node: dict) -> str:
        """深度收集节点的所有文本内容（剥离所有 MD 标记）。"""
        node_type = node.get('type', '')

        if node_type == 'text':
            return node.get('raw', '')
        if node_type == 'softbreak':
            return ' '
        if node_type == 'linebreak':
            return '\n'
        if node_type == 'codespan':
            return node.get('raw', '')
        if node_type == 'html':
            return ''
        if node_type == 'block_html':
            return ''
        if node_type == 'footnote_ref':
            return ''
        if node_type == 'image':
            alt = self._render_children_text(node)
            return f'[图: {alt}]' if alt else '[图]'
        if node_type == 'link':
            return self._render_children_text(node)

        # 格式化标记 — 剥离，只取内容
        if node_type in ('strong', 'emphasis', 'strikethrough'):
            return self._render_children_text(node)

        # 容器节点 — 继续深入
        children = node.get('children', [])
        parts = []
        for child in children:
            if isinstance(child, dict):
                parts.append(self._collect_text(child))
            elif isinstance(child, str):
                parts.append(child)
        return ''.join(parts)

    def _render_children_text(self, node: dict) -> str:
        parts = []
        for child in node.get('children', []):
            parts.append(self._collect_text(child))
        return ''.join(parts)


def parse_markdown(md_text: str) -> tuple[str, list[dict]]:
    """顶层入口：解析 markdown 文本，返回 (结构化文本, TOC列表)。"""
    parser = MdStructuredParser()
    return parser.parse(md_text)
