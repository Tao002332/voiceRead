"""文本处理：分章、分句、上下文语调分析。

语调规则全部是启发式的（不依赖网络/大模型），目标是把“念经”变成“像人读”：
  - 问句：音调上扬、句速略慢、句后停顿加长
  - 感叹：音调上扬、语速加快
  - 引号对话：语速略快、音调略高（和旁白区分）
  - 省略号/破折号：明显加长停顿
  - 短句：放慢，做强调
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- 分章

_CHAPTER_PATTERNS = [
    # 「第一章 甲」「第一章费舍尔」都算标题；但「第一章的内容很精彩。」不算
    # （标题后若紧跟"的/是/中/在…"这类虚词，说明是正文句子）
    re.compile(
        r"^\s*第\s*[零一二三四五六七八九十百千万两0-9]{1,12}\s*[章节回卷部篇集]"
        r"(?!\s*[的是中在里有就又还也便把被让给和与及而但却这那他她它我你们])"
        r"(?:\s*[^\n。！？，、；：\"“”]{0,24})?\s*$"
    ),
    re.compile(r"^\s*(?:CHAPTER|Chapter|chapter)\s*\d{1,4}(?:\s+[^\n。！？，、；：]{0,24})?\s*$"),
    re.compile(r"^\s*(?:序章|楔子|引子|前言|尾声|后记|终章|番外)(?:\s+[^\n。！？，、；：]{0,20})?\s*$"),
    re.compile(r"^\s*[（(【\[]?\s*\d{1,3}\s*[）)】\]]?\s*[、.．]?\s*[^\n]{0,30}$"),
]


def split_chapters(text: str) -> list[tuple[str, str]]:
    """把整本书切成 [(章节标题, 章节正文)]。识别不到章节时按长度均分。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    lines = text.split("\n")
    marks: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 48:
            continue
        if any(p.match(stripped) for p in _CHAPTER_PATTERNS[:3]):
            marks.append((idx, stripped))

    chapters: list[tuple[str, str]] = []
    if len(marks) >= 2:
        # 首个章节标记之前的内容作为“前言”
        if marks[0][0] > 0:
            head = "\n".join(lines[: marks[0][0]]).strip()
            if head:
                chapters.append(("开篇", head))
        for i, (line_no, title) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(lines)
            body = "\n".join(lines[line_no + 1 : end]).strip()
            chapters.append((title, body))
        chapters = [(t, b) for t, b in chapters if b]
        if chapters:
            return chapters

    # 兜底：按 4000 字左右切块，尽量在段落边界断开
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]
    chunk: list[str] = []
    size = 0
    parts: list[tuple[str, str]] = []
    for para in paragraphs:
        chunk.append(para)
        size += len(para)
        if size >= 4000:
            parts.append(("\n\n".join(chunk), size))
            chunk, size = [], 0
    if chunk:
        parts.append(("\n\n".join(chunk), size))
    total = len(parts)
    return [(f"第{i + 1}节 / 共{total}节", body) for i, (body, _) in enumerate(parts)]


def split_paragraphs(chapter_text: str) -> list[str]:
    """章节正文 → 段落列表（保留段落内换行合并）。"""
    raw = re.split(r"\n\s*\n", chapter_text)
    out: list[str] = []
    for block in raw:
        block = block.strip()
        if not block:
            continue
        # 段内单换行通常是排版折行，合并成一行
        out.append(re.sub(r"[ \t]*\n[ \t]*", "", block))
    return out


# ---------------------------------------------------------------- 分句

_SENT_END = "。！？…；!?;"
_CLOSERS = "”’」』）)】》\"'"


def split_sentences(paragraph: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(paragraph)
    while i < n:
        ch = paragraph[i]
        buf.append(ch)
        if ch in _SENT_END:
            j = i + 1
            while j < n and paragraph[j] in _SENT_END + _CLOSERS:
                buf.append(paragraph[j])
                j += 1
            seg = "".join(buf).strip()
            if seg:
                out.append(seg)
            buf = []
            i = j
            continue
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    # 超长句子再按逗号断，避免单次合成过长
    result: list[str] = []
    for seg in out:
        if len(seg) <= 90:
            result.append(seg)
            continue
        pieces = re.split(r"(?<=[，,、])", seg)
        acc = ""
        for piece in pieces:
            acc += piece
            if len(acc) >= 60:
                result.append(acc.strip())
                acc = ""
        if acc.strip():
            result.append(acc.strip())
    return [s for s in result if s.strip()]


# ---------------------------------------------------------------- 语调分析

@dataclass
class Prosody:
    rate: float = 1.0        # 语速倍率
    pitch: int = 0           # 音调偏移（Hz）
    pause_ms: int = 220      # 句后停顿
    kind: str = "narration"  # narration / dialogue / question / exclaim / emphasis

    @property
    def kind_label(self) -> str:
        return {
            "narration": "旁白",
            "dialogue": "对话",
            "question": "疑问",
            "exclaim": "感叹",
            "emphasis": "强调",
            "pause": "停顿",
        }.get(self.kind, self.kind)


_QUESTION_TAIL = re.compile(r"(吗|呢|么|嘛|吧)?\s*[？?]\s*[”’」』）)】》]?$")
_QUESTION_WORDS = re.compile(r"(难道|怎么|为什么|哪里|什么|谁|几|多少|是否|难道说|岂)")
_DIALOGUE_RE = re.compile(r"[“「『\"']")
_EXCLAIM_RE = re.compile(r"[！!]\s*[”’」』）)】》]?$")
_DASH_RE = re.compile(r"[—–]{2,}|——")
_ELLIPSIS_RE = re.compile(r"[…]{1,}|\.{3,}")

# 有实际发音内容的字符（汉字 / 拉丁字母 / 数字）
_SPEAKABLE_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9]")


def is_speakable(text: str) -> bool:
    """判断这句是否值得送去合成。

    小说里 `……`、`——`、`“……”`、`* * *` 这类纯标点句很常见，
    送去 TTS 会报错或返回空音频，必须当成「停顿」而不是「句子」。
    """
    return bool(_SPEAKABLE_RE.search(text or ""))


def analyze(sentence: str, strength: float = 1.0) -> Prosody:
    """根据句子特征给出语速/音调/停顿的建议值。strength 为 0-2 的强度系数。"""
    s = sentence.strip()
    if not s:
        return Prosody()
    if not is_speakable(s):
        # 纯标点：不合成，只留一个明显的停顿
        pause = 700 if _ELLIPSIS_RE.search(s) else 480
        return Prosody(rate=1.0, pitch=0, pause_ms=pause, kind="pause")
    strength = max(0.0, min(2.0, strength))
    rate, pitch, pause = 1.0, 0, 220
    kind = "narration"

    is_dialogue = bool(_DIALOGUE_RE.match(s)) or s.count("“") + s.count("「") > 0
    is_question = bool(_QUESTION_TAIL.search(s)) or bool(_QUESTION_WORDS.search(s) and s.rstrip("”’」』")[-1:] in "？?")
    is_exclaim = bool(_EXCLAIM_RE.search(s))

    if is_exclaim and not is_question:
        kind = "exclaim"
        rate, pitch, pause = 1.0, 4, 320
    elif is_question:
        kind = "question"
        rate, pitch, pause = 1.0, 5, 380
    elif is_dialogue:
        # 对话不再额外加速/升调：模型本身会随引号内容调节，
        # 再叠加人为偏移反而会让「额，那个…」这类口语听起来别扭
        kind = "dialogue"
        rate, pitch, pause = 1.0, 0, 240

    # 语速只由「设置里的基础语速」决定：这里再乘系数会让
    # 朗读实际速度和主界面滑块对不上（用户能听出来）
    if _ELLIPSIS_RE.search(s):
        pause += 260
    if _DASH_RE.search(s):
        pause += 120

    if kind == "narration" and len(s) <= 8:
        kind = "emphasis"
        pause = 300

    # 强度系数只影响音调与停顿；语速恒为 1.0
    rate = 1.0
    pitch = int(pitch * strength)
    pause = int(220 + (pause - 220) * strength)
    return Prosody(rate=rate, pitch=pitch, pause_ms=pause, kind=kind)


# 送进 TTS 前要去掉的首尾包裹符号：
# 引号直接喂给模型，容易让它在开口/收尾做奇怪的升降调（尤以 ……!” 结尾最明显）
_LEADING_WRAP = "“”\"'「」『』‘’（）()【】[]《》 \t\n"
_TRAILING_WRAP = "“”\"'「」『』‘’（）()【】[]《》 \t\n"


def clean_for_tts(sentence: str) -> str:
    """去掉朗读时不需要的符号，保留语气标点（影响韵律）。"""
    s = sentence.replace("\u3000", " ")
    s = re.sub(r"[*#`~^|]+", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = s.strip()
    # 剥掉首尾成对引号/括号，但不动句中标点与内部的省略号
    s = s.lstrip(_LEADING_WRAP)
    s = s.rstrip(_TRAILING_WRAP)
    return s.strip()


# ---------------------------------------------------------------- 章节渲染

@dataclass
class Sentence:
    """带原文偏移的句子，用于阅读区高亮当前朗读位置。"""
    text: str
    start: int
    end: int


def prepare_chapter(chapter_text: str) -> tuple[str, list[Sentence]]:
    """返回 (用于显示的正文, 句子列表)。显示文本中句子偏移与 Sentence.start 一致。"""
    paragraphs = split_paragraphs(chapter_text)
    if not paragraphs:
        paragraphs = [chapter_text.strip()] if chapter_text.strip() else []
    parts: list[str] = []
    sentences: list[Sentence] = []
    cursor = 0
    for idx, para in enumerate(paragraphs):
        if idx:
            parts.append("\n")
            cursor += 1
        for sent in split_sentences(para):
            start = cursor
            parts.append(sent)
            cursor += len(sent)
            sentences.append(Sentence(sent, start, cursor))
    return "".join(parts), sentences
