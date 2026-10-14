"""Offline dictionary hints, not a trained POS tagger or execution authority."""
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import distribution
import json
from pathlib import Path
import re

from bibit_intent.analysis.domain_lexicon import ACTION_VERBS, CONNECTORS


MAX_INPUT = 4096
_PROTECTED = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|「[^」\n]*」'
    r"|(?<![A-Za-z])'[^'\n]*'"
    r'|https?://[^\s,;，。；！？]+|(?:~?/|\./)(?:(?!目录(?:安装|下安装))[^\s,;，。；！？\u4e00-\u9fff])+'
    r'|(?<![A-Za-z0-9_])[A-Za-z0-9_+.-]+\.(?:txt|md|pdf|json|yaml|sh|py|log|deb|csv|tar\.gz|tar\.xz|zip|gz|xz)\b', re.I
)
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_QUESTION = re.compile(r'为什么|为啥|为何|怎么样|怎么办|怎么|如何|怎样|干嘛|哪些|哪个|哪一个|什么|多少|是否|能否|啥|咋|哪|谁|几')
_EN_QUESTION = frozenset('what why how who when where which'.split())
_AUX = frozenset('can could would should may might must shall will do does did is are was were be been have has had'.split())
_POLITE = frozenset('please kindly help want need like 请 麻烦 帮 帮忙 希望 需要 想 要'.split())
_NEGATION = re.compile(r"不要|不用|不必|禁止|勿|别|\b(?:never|without|avoid|don't|shouldn't|do\s+not|should\s+not)\b", re.I)
_CONDITIONAL = re.compile(
    r'如果|假如|除非|否则|一旦|只要|只有|以便|但是|然而|虽然|前提(?:是|为)|条件(?:是|为)|'
    r'只在|仅在|仅当|必须先|确认.{0,24}(?:之后|以后|后再|才能)|'
    r'(?:等|待).{1,24}(?:完成|结束|就绪|成功|充足|足够)(?:之后|以后|后|时)|'
    r'\b(?:if|unless|otherwise|although|provided(?:\s+that)?|providing(?:\s+that)?|but|'
    r'only\s+(?:when|after|once)|as\s+long\s+as|on\s+condition\s+that|'
    r'after|before|until|once)\b', re.I
)
_DOMAIN = frozenset(ACTION_VERBS)
_CHINESE_DOMAIN = tuple(sorted((word for word in _DOMAIN if not word.isascii()), key=lambda word: (-len(word), word)))
_PRODUCTS = frozenset('pip npm apt uv chrome chromium firefox curl vim emacs gcc'.split())
_CONNECTORS = tuple(sorted(CONNECTORS, key=lambda word: (-len(word), word)))
_NOMINAL_SUFFIX = re.compile(r'^(?:记录|历史|日志|目录|路径|位置|列表|次数|速度|状态|按钮|器|工具|管理器|文件夹)')


@dataclass(frozen=True)
class Lexeme:
    text: str
    start: int
    end: int
    pos: str
    lemma: str = ''


def mask_literals(text: str) -> str:
    return _PROTECTED.sub(lambda match: ' ' * len(match.group()), text)


def instruction_search(pattern, text: str):
    """Match syntax outside literals while retaining original named operands."""
    visible = mask_literals(text)
    for match in pattern.finditer(text):
        if visible[match.start():match.end()] == text[match.start():match.end()]:
            return match
        operands = [match.span(name) for name in pattern.groupindex if match.start(name) >= 0]
        if all(text[index] == visible[index] or any(start <= index < end for start, end in operands)
               for index in range(match.start(), match.end())):
            return match
    return None


@lru_cache(maxsize=1)
def dictionaries():
    chinese = {}
    dictionary = distribution('jieba').locate_file('jieba/dict.txt')
    with dictionary.open(encoding='utf-8') as stream:
        for line in stream:
            word, frequency, tag = line.strip().split()
            chinese[word] = tag
    path = Path(__file__).resolve().parents[1] / 'resources/lexicons/wordnet-verbs.json'
    english = json.loads(path.read_text(encoding='utf-8'))
    if (not chinese or not isinstance(english, dict)
            or english.get('version') != 'WordNet-3.0'
            or not isinstance(english.get('verbs'), list) or not english['verbs']
            or not all(isinstance(word, str) and word for word in english['verbs'])
            or not isinstance(english.get('exceptions'), dict)
            or not all(isinstance(forms, list) and forms and all(isinstance(form, str) for form in forms)
                       for forms in english['exceptions'].values())):
        raise ValueError('Invalid offline lexical resource')
    return chinese, frozenset(english['verbs']), english['exceptions']


def english_lemma(word: str, verbs, exceptions) -> str:
    word = word.lower()
    if word in _DOMAIN or word in verbs:
        return word
    candidates = list(exceptions.get(word, ()))
    for suffix, endings in (('ies', ('y',)), ('es', ('', 'e')), ('s', ('',)),
                            ('ied', ('y',)), ('ed', ('', 'e')), ('ing', ('', 'e'))):
        if word.endswith(suffix):
            stem = word[:-len(suffix)]
            candidates.extend(stem + ending for ending in endings)
            if len(stem) > 2 and stem[-1] == stem[-2]:
                candidates.append(stem[:-1])
    return next((candidate for candidate in candidates if candidate in _DOMAIN), '') or next(
        (candidate for candidate in candidates if candidate in verbs), ''
    )


def lexical_analysis(text: str) -> tuple[tuple[Lexeme, ...], str]:
    if len(text) > MAX_INPUT:
        return (), 'input_too_long'
    try:
        chinese, verbs, exceptions = dictionaries()
    except (OSError, ValueError, LookupError, ImportError, TypeError):
        return (), 'lexicon_unavailable'
    visible = mask_literals(text)
    lowered = visible.lower()
    result = []
    cursor = 0
    while cursor < len(visible):
        if visible[cursor].isspace():
            cursor += 1
            continue
        connector = next((word for word in _CONNECTORS if lowered.startswith(word, cursor)
                          and (not word.isascii() or (
                              (cursor == 0 or not lowered[cursor - 1].isalnum())
                              and (cursor + len(word) == len(lowered) or not lowered[cursor + len(word)].isalnum())
                          ))), None)
        if connector:
            result.append(Lexeme(visible[cursor:cursor + len(connector)], cursor, cursor + len(connector), 'CCONJ', connector))
            cursor += len(connector)
            continue
        match = _WORD.match(visible, cursor)
        if match:
            word = match.group()
            lemma = english_lemma(word, verbs, exceptions)
            pos = 'VERB' if lemma else 'X'
            if word.lower() in _AUX:
                pos = 'AUX'
            elif word.lower() in _POLITE:
                pos = 'ADV'
            elif word.lower() in _PRODUCTS:
                pos = 'NOUN'
            elif word.lower() in _EN_QUESTION:
                pos = 'PRON'
            elif word.lower() in {'a', 'an', 'the', 'my', 'your', 'its', 'our', 'their'}:
                pos = 'DET'
            if result and result[-1].pos == 'DET' and pos == 'VERB':
                pos = 'NOUN'
            result.append(Lexeme(word, cursor, match.end(), pos, lemma))
            cursor = match.end()
            continue
        if '\u3400' <= visible[cursor] <= '\u9fff':
            word = next((visible[cursor:end] for end in range(min(len(visible), cursor + 16), cursor, -1)
                         if visible[cursor:end] in chinese or visible[cursor:end] in _DOMAIN), visible[cursor])
            domain_word = next((candidate for candidate in _CHINESE_DOMAIN if visible.startswith(candidate, cursor)), None)
            if domain_word:
                word = domain_word
            elif visible.startswith('请', cursor):
                word = '请'
            tag = chinese.get(word, '')
            pos = 'VERB' if word in _DOMAIN or tag == 'v' else 'NOUN' if tag.startswith('n') or tag == 'vn' else 'X'
            if _QUESTION.fullmatch(word):
                pos = 'PRON'
            elif word in _POLITE:
                pos = 'ADV'
            if pos == 'VERB' and _NOMINAL_SUFFIX.match(visible[cursor + len(word):]) and any(
                previous.pos == 'VERB' for previous in result
            ):
                pos = 'NOUN'
            result.append(Lexeme(word, cursor, cursor + len(word), pos, word))
            cursor += len(word)
            continue
        result.append(Lexeme(visible[cursor], cursor, cursor + 1,
                             'NUM' if visible[cursor].isdigit() else 'PUNCT'))
        cursor += 1
    return tuple(result), ''


def structural_flags(text: str, tokens: tuple[Lexeme, ...]) -> tuple[bool, bool]:
    visible = mask_literals(text)
    conditional = bool(_CONDITIONAL.search(visible))
    negated = any(token.pos == 'VERB' and any(
        match.end() <= token.start and token.start - match.end() <= 14
        and not re.search(r'[,，。;；.!?！？]', visible[match.end():token.start])
        for match in _NEGATION.finditer(visible)) for token in tokens)
    return conditional, negated


def negation_markers(text: str, markers) -> tuple[str, ...]:
    visible = mask_literals(text).lower()
    return tuple(marker for marker in markers if (
        re.search(r'\b' + re.escape(marker) + r'\b', visible) if marker.isascii()
        else marker in visible
    ))


def question_word_count(text: str) -> int:
    visible = mask_literals(text)
    return len(_QUESTION.findall(visible)) + sum(word.lower() in _EN_QUESTION for word in _WORD.findall(visible))
