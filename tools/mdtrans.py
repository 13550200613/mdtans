from collections.abc import Generator
import io
import re
from typing import Any, List
import uuid

from dify_plugin import File, Tool
from dify_plugin.entities.model.llm import LLMModelConfig
from dify_plugin.entities.model.message import SystemPromptMessage, UserPromptMessage
from dify_plugin.entities.tool import ToolInvokeMessage
from dify_plugin.file.file import File

class MarkdownBlock:
    def __init__(self, block_type: str, content, line_num: int = -1):
        self.type = block_type  # text/code/media
        self.content = content  # 文本列表/代码块内容/媒体信息字典
        self.line_num = line_num
        print("1")

def parse_markdown(content: str) -> List[MarkdownBlock]:
    blocks = []
    current_text = []
    in_code_block = False
    media_pattern = re.compile(
        r'^(\s*)(!?\[)(\s*)((?:\n|.)*?)(\s*)(\])(\s*\(\s*(.*?)\s*\)\s*)$', 
        re.DOTALL
    )
    print("2")
    
    lines = content.split('\n')
    for line_num, line in enumerate(lines):
        code_start_match = re.match(r'^\s*```(\w*)', line)
        if code_start_match:
            if current_text:
                blocks.append(MarkdownBlock("text", current_text.copy()))
                current_text = []
            in_code_block = True
            code_content = [line]
            blocks.append(MarkdownBlock("code", code_content, line_num))
            continue
        
        if in_code_block:
            blocks[-1].content.append(line)
            if line.strip() == '```':
                in_code_block = False
            continue
        
        media_match = media_pattern.fullmatch(line)
        if media_match:
            if current_text:
                blocks.append(MarkdownBlock("text", current_text.copy()))
                current_text = []
            alt_text = media_match.group(4).strip()
            blocks.append(MarkdownBlock("media", {
                "raw": line,
                "components": media_match.groups(),
                "alt_text": alt_text
            }, line_num))
            continue
        
        current_text.append(line)
    
    if current_text:
        blocks.append(MarkdownBlock("text", current_text.copy()))
    return blocks

class MdtransTool(Tool):

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        model_info = tool_parameters.get("trans_model")
        file_obj = tool_parameters.get("mdfile")
        query = tool_parameters.get("query", "")
        print("3")
        print("file_obj:",file_obj)

        
        try:
            # 初始化模型配置
            model_config = LLMModelConfig(
                provider=model_info.get("provider"),
                model=model_info.get("model"),
                mode=model_info.get("mode"),
                completion_params=model_info.get("completion_params", {})
            )
            print("4")
            # 获取并处理文件对象
            if not isinstance(file_obj, File):
                raise ValueError("请上传md格式文件")

            else:
                print("5")
                md_bytes_io = io.BytesIO(file_obj.blob)
                print("5.5")
                print("md_bytes_io:",md_bytes_io)
                md_content = md_bytes_io.read().decode('utf-8')
                print("md_content:",md_content)
                if md_content.strip() == "":
                    yield self.create_text_message("文件内容为空")
                    return
                else:
                    yield self.create_text_message(f"文件读取成功")


            # 解析 Markdown
            blocks = parse_markdown(md_content)
            yield self.create_text_message("[进度] Markdown解析完成")
            print("6")

            # 构建翻译索引
            text_index = {}  # (block_idx, line_idx) -> uid
            media_index = {}  # block_idx -> uid
            to_translate = []

            for block_idx, block in enumerate(blocks):
                if block.type == "text":
                    for line_idx, line in enumerate(block.content):
                        if line.strip():
                            uid = str(uuid.uuid4())
                            text_index[(block_idx, line_idx)] = uid
                            to_translate.append((uid, line))
                elif block.type == "media":
                    uid = str(uuid.uuid4())
                    media_index[block_idx] = uid
                    to_translate.append((uid, block.content["alt_text"]))

            # 分块翻译
            CHUNK_SIZE = 3000  # 更保守的分块大小
            translated = []
            for i in range(0, len(to_translate), CHUNK_SIZE):
                chunk = to_translate[i:i+CHUNK_SIZE]
                chunk_texts = [text for _, text in chunk]
                
                # 流式进度报告
                yield self.create_text_message(f"[进度] 正在翻译 {i+1}-{i+len(chunk)} 行")
                
                try:
                    chunk_trans = translate_text(
                        texts=chunk_texts,
                        model_config=model_config,
                        query=query,
                        timeout=30  # 添加超时
                    )
                except Exception as e:
                    yield self.create_text_message(f"分块翻译失败: {str(e)}")
                    return
                
                # 容错处理行数匹配
                valid_trans = chunk_trans[:len(chunk_texts)]
                translated.extend(zip([uid for uid, _ in chunk], valid_trans))

            trans_dict = dict(translated)
            yield self.create_text_message("[进度] 翻译完成，开始重建文档")

            # 重建 Markdown
            output = []
            for block_idx, block in enumerate(blocks):
                if block.type == "code":
                    output.extend(block.content)
                
                elif block.type == "media":
                    uid = media_index.get(block_idx)
                    raw_line = block.content["raw"]
                    
                    if uid and uid in trans_dict:
                        comp = block.content["components"]
                        new_alt = trans_dict[uid]
                        new_line = f"{comp[0]}{comp[1]}{comp[2]}{new_alt}{comp[4]}{comp[5]}{comp[6]}"
                        output.append(new_line)
                    else:
                        output.append(raw_line)
                
                elif block.type == "text":
                    new_lines = []
                    for line_idx, line in enumerate(block.content):
                        uid = text_index.get((block_idx, line_idx))
                        new_line = trans_dict[uid] if uid and uid in trans_dict else line
                        new_lines.append(new_line)
                    output.append("\n".join(new_lines))

                # 阶段性进度报告
                if block_idx % 10 == 0:
                    yield self.create_text_message(f"[进度] 已处理 {block_idx+1}/{len(blocks)} 个区块")

            # 返回最终结果
            print("7")
            yield self.create_blob_message(
                data="\n".join(output).encode('utf-8'),
                meta={
                    "mime_type": "text/markdown",
                    "file_name": "translated.md"
                }
            )

        except Exception as e:
            yield self.create_text_message(f"处理失败: {str(e)}")

def translate_text(
    texts: List[str], 
    model_config: LLMModelConfig,
    query: str = "",
    timeout: int = 30
) -> List[str]:
    """增强翻译函数，添加超时控制"""
    system_prompt = (
        "你是一个专业翻译引擎，请严格遵循：\n"
        "1. 仅翻译文本内容，保留所有格式符号\n"
        "2. 不修改链接、图片路径等非文本内容\n"
        f"{f'3. 用户特别要求: {query}' if query else ''}"
    )
    print("8")

    # 添加超时参数到调用参数
    completion_params = model_config.completion_params.copy()
    completion_params["timeout"] = timeout

    response = model_config.invoke(
        prompt_messages=[
            SystemPromptMessage(system_prompt),
            UserPromptMessage("\n".join(texts))
        ],
        stream=False,
        **completion_params
    )

    if response.status_code != 200:
        raise Exception(f"翻译API错误: {response.code}")

    translated = getattr(response.output, 'text', '')
    return translated.split("\n")[:len(texts)]  # 确保返回行数匹配