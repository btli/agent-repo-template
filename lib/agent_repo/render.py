import re


def neutralize(text: str, max_len: int) -> str:
    while True:
        stripped = re.sub(r"<!--.*?(?:-->|\Z)", "", text, flags=re.DOTALL)
        if stripped == text:
            break
        text = stripped
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return text.replace("@", "@\u200b")[: max(0, max_len)]


def fenced(text: str, max_len: int) -> str:
    text = neutralize(text, max_len)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"
