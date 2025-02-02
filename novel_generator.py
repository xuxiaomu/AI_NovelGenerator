# novel_generator.py
# -*- coding: utf-8 -*-
import os
import logging
import re
from typing import Dict, List, Optional
try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma
from langchain.docstore.document import Document

import nltk
import math
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from utils import (
    read_file, append_text_to_file, clear_file_content,
    save_string_to_txt
)
from prompt_definitions import (
    set_prompt, character_prompt, dark_lines_prompt,
    finalize_setting_prompt, novel_directory_prompt,
    summary_prompt, update_character_state_prompt,
    chapter_outline_prompt, chapter_write_prompt
)
from embedding_ollama import OllamaEmbeddings
from chapter_directory_parser import get_chapter_info_from_directory

# ============ 日志配置 ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def debug_log(prompt: str, response_content: str):
    """打印Prompt与Response，可根据需要保留或去掉。"""
    logging.info(f"\n[Prompt >>>] {prompt}\n")
    logging.info(f"[Response >>>] {response_content}\n")


# ============ 接口判断函数 ============
def is_using_ollama_api(interface_format: str, base_url: str) -> bool:
    """
    当 interface_format == "Ollama" 时返回 True
    """
    if interface_format.lower() == "ollama":
        return True
    return False

def is_using_ml_studio_api(interface_format: str, base_url: str) -> bool:
    """
    如果用户在下拉里选择了 ML Studio
    """
    if interface_format.lower() == "ml studio":
        return True
    return False


def create_embeddings_object(
    api_key: str,
    base_url: str,
    embed_url: str,
    interface_format: str,
    embedding_model_name: str
):
    """
    根据用户在UI中配置的参数，返回对应的 embeddings 对象。
    - 当 interface_format = "Ollama" => OllamaEmbeddings(...)
      （此时把 embed_url 中的 /v1 替换成 /api，以便最后调用 /api/embed）
    - 当 interface_format = "OpenAI" or "ML Studio" => OpenAIEmbeddings
    - 其它情况可自行扩展
    """
    if is_using_ollama_api(interface_format, embed_url):
        # 去除末尾斜杠
        fixed_url = embed_url.rstrip("/")
        # 如果包含 /v1 则替换为 /api
        fixed_url = fixed_url.replace("/v1", "/api")
        return OllamaEmbeddings(
            model_name=embedding_model_name,
            base_url=fixed_url
        )
    elif is_using_ml_studio_api(interface_format, base_url):
        return OpenAIEmbeddings(openai_api_key=api_key, openai_api_base=base_url)
    else:
        # 默认使用 OpenAIEmbeddings
        return OpenAIEmbeddings(openai_api_key=api_key, openai_api_base=base_url)

# ============ 日志配置 ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ============ 向量库相关 ============
VECTOR_STORE_DIR = os.path.join(os.getcwd(), "vectorstore")
if not os.path.exists(VECTOR_STORE_DIR):
    os.makedirs(VECTOR_STORE_DIR)

def clear_vector_store():
    """
    清空本地向量库（删除 vectorstore 文件夹内的内容）。
    """
    if os.path.exists(VECTOR_STORE_DIR):
        try:
            import shutil
            for filename in os.listdir(VECTOR_STORE_DIR):
                file_path = os.path.join(VECTOR_STORE_DIR, filename)
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            logging.info("Local vector store has been cleared.")
        except Exception as e:
            logging.warning(f"Failed to clear vector store: {e}")
    else:
        logging.info("No vector store found to clear.")

def init_vector_store(
    api_key: str,
    base_url: str, 
    interface_format: str,
    embedding_model_name: str,
    texts: List[str], 
    embedding_base_url: str = ""
) -> Chroma:
    """
    初始化并返回一个Chroma向量库，将传入的文本进行嵌入并保存到本地目录。
    embedding_base_url 若不为空，则用于 Ollama 模式下；否则默认使用 base_url。
    """
    embed_url = embedding_base_url if embedding_base_url else base_url
    embeddings = create_embeddings_object(
        api_key=api_key,
        base_url=base_url,
        embed_url=embed_url,
        interface_format=interface_format,
        embedding_model_name=embedding_model_name
    )
    documents = [Document(page_content=t) for t in texts]
    vectorstore = Chroma.from_documents(
        documents,
        embedding=embeddings,
        persist_directory=VECTOR_STORE_DIR
    )
    vectorstore.persist()
    return vectorstore

def load_vector_store(
    api_key: str,
    base_url: str,
    interface_format: str,
    embedding_model_name: str,
    embedding_base_url: str = ""
) -> Optional[Chroma]:
    """
    读取已存在的向量库。若不存在则返回 None。
    """
    if not os.path.exists(VECTOR_STORE_DIR):
        return None
    embed_url = embedding_base_url if embedding_base_url else base_url
    embeddings = create_embeddings_object(
        api_key=api_key,
        base_url=base_url,
        embed_url=embed_url,
        interface_format=interface_format,
        embedding_model_name=embedding_model_name
    )
    return Chroma(persist_directory=VECTOR_STORE_DIR, embedding_function=embeddings)

def update_vector_store(
    api_key: str, 
    base_url: str, 
    new_chapter: str, 
    interface_format: str,
    embedding_model_name: str,
    embedding_base_url: str = ""
) -> None:
    """
    将最新章节文本插入到向量库里，用于后续检索参考。若库不存在则初始化。
    """
    store = load_vector_store(
        api_key=api_key,
        base_url=base_url,
        interface_format=interface_format,
        embedding_model_name=embedding_model_name,
        embedding_base_url=embedding_base_url
    )
    if not store:
        logging.info("Vector store does not exist. Initializing a new one...")
        init_vector_store(
            api_key=api_key,
            base_url=base_url,
            interface_format=interface_format,
            embedding_model_name=embedding_model_name,
            texts=[new_chapter],
            embedding_base_url=embedding_base_url
        )
        return

    new_doc = Document(page_content=new_chapter)
    store.add_documents([new_doc])
    store.persist()

def get_relevant_context_from_vector_store(
    api_key: str,
    base_url: str,
    query: str,
    interface_format: str,
    embedding_model_name: str,
    embedding_base_url: str = "",
    k: int = 2
) -> str:
    """
    从向量库中检索与 query 最相关的 k 条文本，拼接后返回。
    若向量库不存在则返回空字符串。
    """
    store = load_vector_store(
        api_key=api_key,
        base_url=base_url,
        interface_format=interface_format,
        embedding_model_name=embedding_model_name,
        embedding_base_url=embedding_base_url
    )
    if not store:
        logging.warning("Vector store not found. Returning empty context.")
        return ""
    docs = store.similarity_search(query, k=k)
    combined = "\n".join([d.page_content for d in docs])
    return combined


# ============ 多步生成：设置 & 目录 ============

class OverallState(TypedDict):
    topic: str
    genre: str
    number_of_chapters: int
    word_number: int
    novel_setting_base: str
    character_setting: str
    dark_lines: str
    final_novel_setting: str
    novel_directory: str

def Novel_novel_directory_generate(
    api_key: str,
    base_url: str,
    llm_model: str,
    topic: str,
    genre: str,
    number_of_chapters: int,
    word_number: int,
    filepath: str,
    temperature: float = 0.7
) -> None:
    """
    使用多步流程，生成 Novel_setting.txt 与 Novel_directory.txt 并保存到 filepath。
    """
    # 确保文件夹存在
    os.makedirs(filepath, exist_ok=True)

    model = ChatOpenAI(
        model=llm_model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature
    )

    def generate_base_setting(state: OverallState) -> Dict[str, str]:
        prompt = set_prompt.format(
            topic=state["topic"],
            genre=state["genre"],
            number_of_chapters=state["number_of_chapters"],
            word_number=state["word_number"]
        )
        response = model.invoke(prompt)
        if not response:
            logging.warning("generate_base_setting: No response.")
            return {"novel_setting_base": ""}
        debug_log(prompt, response.content)
        return {"novel_setting_base": response.content.strip()}

    def generate_character_setting(state: OverallState) -> Dict[str, str]:
        prompt = character_prompt.format(
            novel_setting=state["novel_setting_base"]
        )
        response = model.invoke(prompt)
        if not response:
            logging.warning("generate_character_setting: No response.")
            return {"character_setting": ""}
        debug_log(prompt, response.content)
        return {"character_setting": response.content.strip()}

    def generate_dark_lines(state: OverallState) -> Dict[str, str]:
        prompt = dark_lines_prompt.format(
            character_info=state["character_setting"]
        )
        response = model.invoke(prompt)
        if not response:
            logging.warning("generate_dark_lines: No response.")
            return {"dark_lines": ""}
        debug_log(prompt, response.content)
        return {"dark_lines": response.content.strip()}

    def finalize_novel_setting(state: OverallState) -> Dict[str, str]:
        prompt = finalize_setting_prompt.format(
            novel_setting_base=state["novel_setting_base"],
            character_setting=state["character_setting"],
            dark_lines=state["dark_lines"]
        )
        response = model.invoke(prompt)
        if not response:
            logging.warning("finalize_novel_setting: No response.")
            return {"final_novel_setting": ""}
        debug_log(prompt, response.content)
        return {"final_novel_setting": response.content.strip()}

    def generate_novel_directory(state: OverallState) -> Dict[str, str]:
        prompt = novel_directory_prompt.format(
            final_novel_setting=state["final_novel_setting"],
            number_of_chapters=state["number_of_chapters"]
        )
        response = model.invoke(prompt)
        if not response:
            logging.warning("generate_novel_directory: No response.")
            return {"novel_directory": ""}
        debug_log(prompt, response.content)
        return {"novel_directory": response.content.strip()}

    # 构建状态图
    graph = StateGraph(OverallState)
    graph.add_node("generate_base_setting", generate_base_setting)
    graph.add_node("generate_character_setting", generate_character_setting)
    graph.add_node("generate_dark_lines", generate_dark_lines)
    graph.add_node("finalize_novel_setting", finalize_novel_setting)
    graph.add_node("generate_novel_directory", generate_novel_directory)

    graph.add_edge(START, "generate_base_setting")
    graph.add_edge("generate_base_setting", "generate_character_setting")
    graph.add_edge("generate_character_setting", "generate_dark_lines")
    graph.add_edge("generate_dark_lines", "finalize_novel_setting")
    graph.add_edge("finalize_novel_setting", "generate_novel_directory")
    graph.add_edge("generate_novel_directory", END)

    app = graph.compile()

    input_params = {
        "topic": topic,
        "genre": genre,
        "number_of_chapters": number_of_chapters,
        "word_number": word_number
    }
    result = app.invoke(input_params)

    if not result:
        logging.warning("Novel_novel_directory_generate: invoke() 结果为空，生成失败。")
        return

    final_novel_setting = result.get("final_novel_setting", "")
    final_novel_directory = result.get("novel_directory", "")

    if not final_novel_setting or not final_novel_directory:
        logging.warning("生成失败：缺少 final_novel_setting 或 novel_directory。")
        return

    # 写入文件
    filename_set = os.path.join(filepath, "Novel_setting.txt")
    filename_novel_directory = os.path.join(filepath, "Novel_directory.txt")

    def clean_text(txt: str) -> str:
        return txt.replace('#', '').replace('*', '')

    final_novel_setting_cleaned = clean_text(final_novel_setting)
    final_novel_directory_cleaned = clean_text(final_novel_directory)

    append_text_to_file(final_novel_setting_cleaned, filename_set)
    append_text_to_file(final_novel_directory_cleaned, filename_novel_directory)

    logging.info("Novel settings and directory generated successfully.")


# ============ 获取最近N章内容，生成短期摘要 ============

def get_last_n_chapters_text(chapters_dir: str, current_chapter_num: int, n: int = 3) -> List[str]:
    """
    从指定文件夹中，读取最近 n 章的内容（如果存在），并按从旧到新的顺序返回文本列表。
    不包含当前章，只拿之前的 n 章。
    """
    texts = []
    start_chap = max(1, current_chapter_num - n)
    for c in range(start_chap, current_chapter_num):
        chap_file = os.path.join(chapters_dir, f"chapter_{c}.txt")
        if os.path.exists(chap_file):
            text = read_file(chap_file).strip()
            if text:
                texts.append(text)
    return texts

def summarize_recent_chapters(
        llm_model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        chapters_text_list: List[str]
    ) -> str:
    """
    将最近几章的文本拼接后，通过模型生成一个相对详细的“短期内容摘要”。
    如果没有可用的模型（model=None），则退化为简单截断示例。
    """
    model = ChatOpenAI(
        model=llm_model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature
    )

    if not chapters_text_list:
        return ""

    combined_text = "\n".join(chapters_text_list)
    # 如果未传入model，就做个简单的退化输出
    if not model:
        return f"【摘要-演示】\n{combined_text[:800]}..."

    # 构造一个提示词（Prompt），指示模型生成精简摘要
    prompt = f"""你是一名资深的长篇小说写作辅助AI。下面是最近几章的合并文本内容：
{combined_text}

请你为此文本生成一段简洁扼要的摘要，突出主要剧情进展、角色变化、冲突焦点等要点。
1.请用中文输出，不超过500字。
2.仅回复摘要内容，不需要其他信息。
"""
    # 调用模型获取摘要
    response = model.invoke(prompt)
    if not response or not response.content.strip():
        # 若模型无响应或空，返回简单截断
        return f"【摘要-演示】\n{combined_text[:800]}..."

    # 返回模型生成的摘要文本
    return response.content.strip()


# ============ 新增：更新剧情要点/未解决冲突 ============

PLOT_ARCS_PROMPT = """\
下面是新生成的章节内容:
{chapter_text}

这里是已记录的剧情要点/未解决冲突(可能为空):
{old_plot_arcs}

请基于新的章节内容，提炼出本章引入或延续的悬念、冲突、角色暗线等，将其合并到旧的剧情要点中。
若有新的冲突则添加，若有已解决/不再重要的冲突可标注或移除。
最终输出一份更新后的剧情要点列表，以帮助后续保持故事的整体一致性和悬念延续。
"""

def update_plot_arcs(
    chapter_text: str,
    old_plot_arcs: str,
    api_key: str,
    base_url: str,
    model_name: str,
    temperature: float
) -> str:
    """
    利用模型分析最新章节文本，提炼或更新“未解决冲突或剧情要点”。
    并返回更新后的字符串。
    """
    model = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature
    )
    prompt = PLOT_ARCS_PROMPT.format(
        chapter_text=chapter_text,
        old_plot_arcs=old_plot_arcs
    )
    response = model.invoke(prompt)
    if not response:
        logging.warning("update_plot_arcs: No response.")
        return old_plot_arcs
    debug_log(prompt, response.content)
    return response.content.strip()


# ============ 生成章节草稿 & 定稿 ============

def generate_chapter_draft(
    novel_settings: str,
    global_summary: str,
    character_state: str,
    recent_chapters_summary: str,
    user_guidance: str,
    api_key: str,
    base_url: str,
    model_name: str,
    novel_number: int,
    word_number: int,
    temperature: float,
    novel_novel_directory: str,
    filepath: str
) -> str:
    """
    仅生成当前章节的草稿，不更新全局摘要/角色状态/向量库。
    并将生成的内容写到 "chapter_{novel_number}.txt" 覆盖写入。
    同时生成 "outline_{novel_number}.txt" 存储大纲内容。
    """
    # 0) 根据 novel_number 从 novel_novel_directory 中获取本章标题及简述
    chapter_info = get_chapter_info_from_directory(novel_novel_directory, novel_number)
    chapter_title = chapter_info["chapter_title"]
    chapter_brief = chapter_info["chapter_brief"]

    # 1) 从向量库检索上下文 (此处仅演示 query="回顾剧情")
    relevant_context = get_relevant_context_from_vector_store(
        api_key=api_key,
        base_url=base_url,
        query="回顾剧情",
        interface_format="OpenAI",
        embedding_model_name="",
        embedding_base_url="",
        k=2
    )

    model = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature
    )

    # 2) 生成大纲
    outline_prompt_text = chapter_outline_prompt.format(
        novel_setting=novel_settings,
        character_state=character_state + "\n\n【历史上下文】\n" + relevant_context,
        global_summary=global_summary,
        novel_number=novel_number,
        chapter_title=chapter_title,
        chapter_brief=chapter_brief
    )
    outline_prompt_text += f"\n\n【最近几章摘要】\n{recent_chapters_summary}"
    outline_prompt_text += f"\n\n【用户指导】\n{user_guidance if user_guidance else '（无）'}"

    response_outline = model.invoke(outline_prompt_text)
    chapter_outline = response_outline.content.strip() if response_outline else ""

    outlines_dir = os.path.join(filepath, "outlines")
    os.makedirs(outlines_dir, exist_ok=True)
    outline_file = os.path.join(outlines_dir, f"outline_{novel_number}.txt")
    clear_file_content(outline_file)
    save_string_to_txt(chapter_outline, outline_file)

    # 3) 生成正文草稿
    writing_prompt_text = chapter_write_prompt.format(
        novel_setting=novel_settings,
        character_state=character_state + "\n\n【历史上下文】\n" + relevant_context,
        global_summary=global_summary,
        chapter_outline=chapter_outline,
        word_number=word_number,
        chapter_title=chapter_title,
        chapter_brief=chapter_brief
    )
    writing_prompt_text += f"\n\n【最近几章摘要】\n{recent_chapters_summary}"
    writing_prompt_text += f"\n\n【用户指导】\n{user_guidance if user_guidance else '（无）'}"

    response_chapter = model.invoke(writing_prompt_text)
    chapter_content = response_chapter.content.strip() if response_chapter else ""

    chapters_dir = os.path.join(filepath, "chapters")
    os.makedirs(chapters_dir, exist_ok=True)
    chapter_file = os.path.join(chapters_dir, f"chapter_{novel_number}.txt")
    clear_file_content(chapter_file)
    save_string_to_txt(chapter_content, chapter_file)

    logging.info(f"[Draft] Chapter {novel_number} generated as a draft.")
    return chapter_content

def finalize_chapter(
    novel_number: int,
    word_number: int,
    api_key: str,
    base_url: str,
    interface_format: str,
    embedding_model_name: str,
    model_name: str,
    temperature: float,
    filepath: str
):
    """
    对当前章节进行定稿：
    1. 读取 chapter_{novel_number}.txt 的最终内容；
    2. 更新全局摘要、角色状态文件；
    3. 如果字数明显少于 word_number 的 80%，则自动调用 enrich_chapter_text 再次扩写；
    4. 更新向量库；
    5. 新增：更新剧情要点/未解决冲突 -> plot_arcs.txt
    """
    # 读取当前章节内容
    chapters_dir = os.path.join(filepath, "chapters")
    chapter_file = os.path.join(chapters_dir, f"chapter_{novel_number}.txt")
    chapter_text = read_file(chapter_file).strip()
    if not chapter_text:
        logging.warning(f"Chapter {novel_number} is empty, cannot finalize.")
        return

    character_state_file = os.path.join(filepath, "character_state.txt")
    global_summary_file = os.path.join(filepath, "global_summary.txt")
    plot_arcs_file = os.path.join(filepath, "plot_arcs.txt")

    old_char_state = read_file(character_state_file)
    old_global_summary = read_file(global_summary_file)
    old_plot_arcs = read_file(plot_arcs_file)

    # 1) 若字数明显不足，做 enrich
    if len(chapter_text) < 0.8 * word_number:
        logging.info("Chapter text seems shorter than 80% of desired length. Attempting to enrich content...")
        chapter_text = enrich_chapter_text(
            chapter_text=chapter_text,
            word_number=word_number,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            temperature=temperature
        )
        clear_file_content(chapter_file)
        save_string_to_txt(chapter_text, chapter_file)
        logging.info("Chapter text has been enriched and updated.")

    # 2) 更新全局摘要
    model = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature
    )

    def update_global_summary(chapter_text: str, old_summary: str) -> str:
        prompt = summary_prompt.format(
            chapter_text=chapter_text,
            global_summary=old_summary
        )
        response = model.invoke(prompt)
        return response.content.strip() if response else old_summary

    new_global_summary = update_global_summary(chapter_text, old_global_summary)

    # 3) 更新角色状态
    def update_character_state(chapter_text: str, old_state: str) -> str:
        prompt = update_character_state_prompt.format(
            chapter_text=chapter_text,
            old_state=old_state
        )
        response = model.invoke(prompt)
        return response.content.strip() if response else old_state

    new_char_state = update_character_state(chapter_text, old_char_state)

    # 4) 更新剧情要点
    new_plot_arcs = update_plot_arcs(
        chapter_text=chapter_text,
        old_plot_arcs=old_plot_arcs,
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        temperature=temperature
    )

    # 5) 覆盖写入文件
    clear_file_content(character_state_file)
    save_string_to_txt(new_char_state, character_state_file)

    clear_file_content(global_summary_file)
    save_string_to_txt(new_global_summary, global_summary_file)

    clear_file_content(plot_arcs_file)
    save_string_to_txt(new_plot_arcs, plot_arcs_file)

    # 6) 更新向量库
    update_vector_store(
        api_key=api_key, 
        base_url=base_url, 
        new_chapter=chapter_text,
        interface_format=interface_format,
        embedding_model_name=embedding_model_name
    )

    logging.info(f"Chapter {novel_number} has been finalized.")

def enrich_chapter_text(
    chapter_text: str,
    word_number: int,
    api_key: str,
    base_url: str,
    model_name: str,
    temperature: float
) -> str:
    """
    当章节篇幅不足时，调用此函数对章节文本进行二次扩写。
    可以让模型补充场景描写、角色心理等，保证与现有文本风格一致。
    """
    model = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature
    )
    prompt = f"""以下是当前章节文本，可能篇幅较短，请在保持剧情连贯的前提下进行扩写，使其更充实、生动，并尽量靠近目标 {word_number} 字数。

原章节内容：
{chapter_text}"""

    response = model.invoke(prompt)
    if not response:
        return chapter_text
    return response.content.strip()

# ============ 导入外部知识文本 ============

def import_knowledge_file(
        api_key: str,
        base_url: str, 
        interface_format: str,
        embedding_model_name: str,
        file_path: str, 
        embedding_base_url: str = ""
    ) -> None:
    """
    将用户选定的文本文件导入到向量库，以便在写作时检索。
    """
    logging.info(f"开始导入知识库文件: {file_path}，当前接口格式: {interface_format}，当前模型: {embedding_model_name}")
    if not os.path.exists(file_path):
        logging.warning(f"知识库文件不存在: {file_path}")
        return

    content = read_file(file_path)
    if not content.strip():
        logging.warning("知识库文件内容为空。")
        return

    paragraphs = advanced_split_content(content)

    store = load_vector_store(api_key, base_url, interface_format, embedding_model_name, embedding_base_url)
    if not store:
        logging.info("Vector store does not exist. Initializing a new one for knowledge import...")
        init_vector_store(
            api_key,
            base_url,
            interface_format,
            embedding_model_name,
            paragraphs,
            embedding_base_url
        )
        return

    docs = [Document(page_content=p) for p in paragraphs]
    store.add_documents(docs)
    store.persist()
    logging.info("知识库文件已成功导入至向量库。")

def advanced_split_content(content: str,
                           similarity_threshold: float = 0.7,
                           max_length: int = 500) -> List[str]:
    """
    将文本先按句子切分，然后根据语义相似度进行合并，最后根据max_length进行二次切分。
    """
    nltk.download('punkt_tab', quiet=True)
    sentences = nltk.sent_tokenize(content)

    if not sentences:
        return []

    model = SentenceTransformer('paraphrase-MiniLM-L6-v2')
    embeddings = model.encode(sentences)

    merged_paragraphs = []
    current_sentences = [sentences[0]]
    current_embedding = embeddings[0]

    for i in range(1, len(sentences)):
        sim = cosine_similarity([current_embedding], [embeddings[i]])[0][0]
        if sim >= similarity_threshold:
            current_sentences.append(sentences[i])
            current_embedding = (current_embedding + embeddings[i]) / 2.0
        else:
            merged_paragraphs.append(" ".join(current_sentences))
            current_sentences = [sentences[i]]
            current_embedding = embeddings[i]

    if current_sentences:
        merged_paragraphs.append(" ".join(current_sentences))

    final_segments = []
    for para in merged_paragraphs:
        if len(para) > max_length:
            sub_segments = split_by_length(para, max_length=max_length)
            final_segments.extend(sub_segments)
        else:
            final_segments.append(para)

    return final_segments

def split_by_length(text: str, max_length: int = 500) -> List[str]:
    segments = []
    start_idx = 0
    while start_idx < len(text):
        end_idx = min(start_idx + max_length, len(text))
        segment = text[start_idx:end_idx]
        segments.append(segment.strip())
        start_idx = end_idx
    return segments
