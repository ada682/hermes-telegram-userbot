"""Mirror native Hermes context truncation for userbot bridge.

Native behavior references:
- agent/prompt_builder.py:_truncate_content / load_soul_md / build_context_files_prompt
- agent/context_engine.py: sanitize_memory_context
"""

CONTEXT_FILE_MAX_CHARS = 20_000
CONTEXT_TRUNCATE_HEAD_RATIO = 0.7
CONTEXT_TRUNCATE_TAIL_RATIO = 0.2
MEMORY_CONTEXT_MAX_CHARS = 6_000
_MEMORY_CONTEXT_HEAD_CHARS = 4_000
_MEMORY_CONTEXT_TAIL_CHARS = 1_500


def truncate(content: str, filename: str, max_chars: int = CONTEXT_FILE_MAX_CHARS) -> str:
    if max_chars is None:
        max_chars = CONTEXT_FILE_MAX_CHARS
    if len(content) <= max_chars:
        return content
    head_chars = int(max_chars * CONTEXT_TRUNCATE_HEAD_RATIO)
    tail_chars = int(max_chars * CONTEXT_TRUNCATE_TAIL_RATIO)
    head = content[:head_chars]
    tail = content[-tail_chars:]
    marker = (
        "\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of "
        "{total} chars. The middle is omitted — if you need the full "
        "instructions, read the complete file with the read_file tool: "
        "{filename}]\n\n"
    ).format(
        filename=filename,
        head_chars=head_chars,
        tail_chars=tail_chars,
        total=len(content),
    )
    return head + marker + tail


def truncate_memory_context(content: str) -> str:
    if len(content) <= MEMORY_CONTEXT_MAX_CHARS:
        return content
    head = content[:_MEMORY_CONTEXT_HEAD_CHARS]
    tail = content[-_MEMORY_CONTEXT_TAIL_CHARS:]
    marker = "\n...[memory provider context truncated]...\n"
    return head + marker + tail


def read_context_file(path: str, filename: str, max_chars: int = CONTEXT_FILE_MAX_CHARS) -> str:
    try:
        content = open(path, encoding="utf-8").read().strip()
        if not content:
            return ""
        return truncate(content, filename, max_chars)
    except Exception:
        return ""


def read_memory_file(path: str) -> str:
    try:
        content = open(path, encoding="utf-8").read().strip()
        if not content:
            return ""
        return truncate_memory_context(content)
    except Exception:
        return ""
